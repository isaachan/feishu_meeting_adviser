"""Recorder CLI: fake script replay and real audio capture."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
import time

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
    real.add_argument('--event',
                      help='bound Feishu calendar event id (<calendar_id>/<event_id>) '
                           'from the organizer-authorized meeting selection; written '
                           'as stream metadata and sent as STT auth when configured')
    real.add_argument('--oauth-dir', type=Path,
                      help='recorder-private data dir with config.json/oauth tokens '
                           '(default: module data/)')
    auth = commands.add_parser(
        'auth', help='Feishu user OAuth for meeting selection')
    auth.add_argument('--oauth-dir', type=Path,
                      help='recorder-private data dir (default: module data/)')
    auth_commands = auth.add_subparsers(dest='auth_command')
    login = auth_commands.add_parser('login', help='authorize via the local browser flow')
    login.add_argument('--port', type=int, default=8765,
                       help='local OAuth callback port; must match the redirect URI '
                            'registered on the Feishu app (default 8765)')
    login.add_argument('--timeout', type=int, choices=range(30, 1801), default=600,
                       metavar='30..1800')
    auth_commands.add_parser('status', help='show local authorization state')
    auth_commands.add_parser('logout', help='forget local credentials')
    meetings = commands.add_parser(
        'meetings', help='list current-window Feishu meetings with organizer flags')
    meetings.add_argument('--window', type=int, default=1800,
                          help='slack in seconds at both ends of now (default 1800)')
    meetings.add_argument('--json', action='store_true', help='print the raw report')
    meetings.add_argument('--oauth-dir', type=Path,
                          help='recorder-private data dir (default: module data/)')
    return root


def _default_data_root() -> Path:
    return Path(__file__).resolve().parents[2].parent / 'data'


def _default_oauth_dir() -> Path:
    return Path(__file__).resolve().parents[2] / 'data'


def _oauth_manager(oauth_dir: Path | None):
    from .oauth import OAuthManager, load_credentials

    directory = oauth_dir or _default_oauth_dir()
    return OAuthManager(load_credentials(directory), directory)


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


def _format_meeting_line(index: int, meeting: dict) -> str:
    role = '组织者' if meeting['is_organizer'] else '参会者'
    start = time.strftime('%m-%d %H:%M', time.localtime(meeting['start_ts']))
    end = time.strftime('%H:%M', time.localtime(meeting['end_ts']))
    online = '' if meeting['online_meeting'] else '（无视频会议）'
    return (f"{index}. [{role}] {start}-{end} {meeting['summary'] or '（无标题）'}"
            f"{online}  event_id={meeting['event_id']}")


def _auth_command(args) -> int:
    from .oauth import login as oauth_login

    manager = _oauth_manager(args.oauth_dir)
    if args.auth_command == 'login':
        result = oauth_login(manager, port=args.port, timeout=args.timeout,
                             notify=lambda value: print(value, flush=True))
    elif args.auth_command == 'status':
        result = manager.status()
    elif args.auth_command == 'logout':
        result = manager.logout()
    else:
        print('Usage: recorder auth login|status|logout', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _meetings_command(args) -> int:
    from .feishu_calendar import MeetingCalendar

    report = MeetingCalendar(_oauth_manager(args.oauth_dir)).my_meetings(
        window_seconds=args.window)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"当前时间窗 ±{args.window // 60} 分钟，日历 {report['calendar_id'] or '未找到'}")
        if not report['meetings']:
            print('没有重叠的会议。')
        for index, meeting in enumerate(report['meetings'], start=1):
            print(_format_meeting_line(index, meeting))
    return 0


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
        if args.command == 'auth':
            return _auth_command(args)
        if args.command == 'meetings':
            return _meetings_command(args)
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
            stt_auth = stt_auth_refresh = None
            if args.event and args.stt == 'remote-stream':
                from .oauth import stt_auth_provider

                payload, on_expired = stt_auth_provider(
                    _oauth_manager(args.oauth_dir), args.event)
                stt_auth, stt_auth_refresh = payload, on_expired
            recorder = RealRecorder(session_id=args.session, data_root=data_root,
                                    mic_enabled=args.mic,
                                    mic_speaker=args.mic_speaker,
                                    stt_mode=args.stt,
                                    stt_model_dir=args.stt_model_dir,
                                    stt_url=args.stt_url,
                                    calendar_event_id=args.event,
                                    stt_auth=stt_auth,
                                    stt_auth_refresh=stt_auth_refresh)
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
    except Exception as exc:  # noqa: BLE001 - safe provider messages only
        from .feishu_calendar import CalendarError
        from .oauth import OAuthError

        if isinstance(exc, (OAuthError, CalendarError)):
            if isinstance(exc, OAuthError) and exc.reason == 'oauth_not_authorized':
                print('Operation stopped: missing Feishu app credentials. '
                      'Set LMR_FEISHU_APP_ID/LMR_FEISHU_APP_SECRET or create '
                      'config.json in the recorder data dir.', file=sys.stderr)
                return 1
            print('Operation stopped: ' + str(exc), file=sys.stderr)
            return 1
        raise
    if summary is not None:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
