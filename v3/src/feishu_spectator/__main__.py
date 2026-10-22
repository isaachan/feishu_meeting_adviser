"""Explicit local CLI for spectator mode. Credentials come only from YAML."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import sys

from .client import ApiError, SpectatorClient, StopRequested
from .collector import ListenerStopped, PageFormatError, Spectator
from .config import ConfigError, DEFAULT_CONFIG, load_config
from .infrastructure import LifecycleError
from .models import digest
from .oauth import OAuthError, OAuthManager, OAuthTransport
from .storage import CapacityError, StorageError, Store

VERSION = '0.1.0'


def _oauth_manager(config):
    return OAuthManager(config.credentials, config.data_directory,
                        transport=OAuthTransport(config.credentials, timeout=config.timeout_seconds))


def _client(config, manager):
    return SpectatorClient(manager.provider(), timeout=config.timeout_seconds,
                           request_seconds=config.request_seconds)


def _database_path(config) -> Path:
    return config.data_directory / 'spectator.sqlite3'


def _print_item(item):
    print(json.dumps(item.to_dict(), ensure_ascii=False), flush=True)


def _resolve_meeting(config, args) -> str:
    manager = _oauth_manager(config)
    meetings = _client(config, manager).active_meetings()
    if not meetings:
        raise LifecycleError('no active meeting; join the Feishu meeting first, '
                             'and confirm "allow agents to join" is enabled for it')
    chosen = meetings[0]
    if args.meeting_id:
        for meeting in meetings:
            if meeting.meeting_id == args.meeting_id:
                chosen = meeting
                break
        else:
            raise LifecycleError('the requested meeting_id is not among your active meetings')
    elif len(meetings) > 1:
        print(json.dumps([m.to_dict() for m in meetings], ensure_ascii=False), flush=True)
        print(f'multiple active meetings; using {chosen.meeting_id} '
              '(pass --meeting-id to choose another)', file=sys.stderr, flush=True)
    return chosen.meeting_id


def handle_meetings(context, args):
    meetings = _client(context['config'], _oauth_manager(context['config'])).active_meetings()
    print(json.dumps([m.to_dict() for m in meetings], ensure_ascii=False))
    return 0


def handle_listen(context, args):
    config = context['config']
    manager = _oauth_manager(config)
    meeting_id = args.meeting_id or _resolve_meeting(config, args)
    Store.initialize(_database_path(config), max_database_bytes=config.max_database_bytes)
    store = Store(_database_path(config), max_database_bytes=config.max_database_bytes)
    stop = context['stop']
    try:
        spectator = Spectator(store, meeting_id, _client(config, manager),
                              settings=config.listen, should_stop=stop,
                              diagnose=context['diagnose'],
                              on_item=_print_item if args.show_text else None)
        print(json.dumps({'listening': True, 'meeting_hash': digest(meeting_id)[:12],
                          'stop': 'Ctrl-C stops polling only; you stay in the meeting'}),
              flush=True)
        if args.once:
            print(json.dumps(spectator.poll_once()))
        else:
            spectator.run()
        checkpoint = store.checkpoint(meeting_id)
        print(json.dumps({'state': 'stopped', 'pages': checkpoint['pages'],
                          'cursor_reset_count': checkpoint['cursor_reset_count']}))
        return 0
    finally:
        store.close()


def handle_status(context, args):
    config = context['config']
    path = _database_path(config)
    if not path.exists():
        print(json.dumps({'meetings': {}}))
        return 0
    store = Store(path, readonly=True)
    try:
        rows = store.db.execute('SELECT meeting_id FROM meetings').fetchall()
        result = {}
        for row in rows:
            meeting_id = row['meeting_id']
            checkpoint = store.checkpoint(meeting_id)
            result[digest(meeting_id)[:12]] = {
                'pages': checkpoint.get('pages'),
                'has_cursor': bool(checkpoint.get('page_token')),
                'cursor_reset_count': checkpoint.get('cursor_reset_count'),
                'last_success': checkpoint.get('last_success'),
                **store.statistics(meeting_id)['stored_items'],
            }
        print(json.dumps({'meetings': result}))
        return 0
    finally:
        store.close()


def handle_recent(context, args):
    config = context['config']
    path = _database_path(config)
    if not path.exists():
        print('[]')
        return 0
    store = Store(path, readonly=True)
    try:
        rows = store.db.execute('SELECT meeting_id FROM meetings').fetchall()
        items = []
        for row in rows:
            items.extend(store.recent_items(row['meeting_id'], limit=args.limit))
        items.sort(key=lambda item: str(item.get('received_at')), reverse=True)
        print(json.dumps(items[:args.limit], ensure_ascii=False))
        return 0
    finally:
        store.close()


COMMAND_HANDLERS = {
    'meetings': handle_meetings,
    'listen': handle_listen,
    'status': handle_status,
    'recent': handle_recent,
}


def parser():
    root = argparse.ArgumentParser(
        description='Feishu spectator mode: listen to a meeting as the local user, no bot joins')
    root.add_argument('--version', action='version', version=VERSION)
    root.add_argument('--config', type=Path, default=DEFAULT_CONFIG,
                      help='private local YAML configuration')
    commands = root.add_subparsers(dest='command')
    commands.add_parser('meetings', help='list meetings you are currently in')
    listen = commands.add_parser('listen', help='poll captions/chat/participants continuously; Ctrl-C stops polling only')
    listen.add_argument('--meeting-id', help='long meeting ID from `meetings`; defaults to the first active meeting')
    listen.add_argument('--once', action='store_true', help='drain pending pages once and exit')
    listen.add_argument('--no-text', dest='show_text', action='store_false', default=True,
                        help='do not print item text to the terminal')
    commands.add_parser('status', help='checkpoint and stored-item statistics (identifiers hashed)')
    recent = commands.add_parser('recent', help='print recently stored items as JSON; requires --show-text')
    recent.add_argument('--limit', type=int, default=50)
    recent.add_argument('--show-text', action='store_true', required=True,
                        help='explicitly allow printing stored meeting content')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    stop = {'requested': False}

    def request_stop(*_):
        stop['requested'] = True

    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(received, request_stop)
        except (ValueError, OSError):
            pass
    diagnostics = []
    try:
        if args.command == 'listen':
            import os
            os.umask(0o077)
        config = load_config(args.config)
        context = {'config': config, 'stop': lambda: stop['requested'],
                   'diagnose': diagnostics.append}
        return COMMAND_HANDLERS[args.command](context, args)
    except ListenerStopped as exc:
        print(f'Listening stopped ({exc.state}): {exc.reason}. Check that you are still in the '
              'meeting and "allow agents to join" is enabled.', file=sys.stderr)
        return 1
    except (ConfigError, StorageError, CapacityError, LifecycleError, ApiError,
            OAuthError, PageFormatError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print('Operation stopped: local file/database error; check private storage.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, StopRequested):
        print('Operation interrupted; polling stops, you remain in the meeting.', file=sys.stderr)
        return 130
    finally:
        for record in diagnostics:
            print(json.dumps(record), file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
