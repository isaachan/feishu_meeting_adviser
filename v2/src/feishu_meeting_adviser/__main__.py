"""Explicit local CLI. Credentials and meeting number are read only from YAML."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading

from . import __version__
from .client import ApiError, FeishuClient, StopRequested
from .collector import utc_now
from .config import ConfigError, DEFAULT_CONFIG, load_config
from .lifecycle import LifecycleError, MeetingSource, SessionManager
from .models import SourceState, digest
from .storage import DurableFakeConsumer, StorageError, Store


def parser():
    root = argparse.ArgumentParser(description='Durable Feishu meeting transcript source')
    root.add_argument('--version', action='version', version=__version__)
    root.add_argument('--config', type=Path, default=DEFAULT_CONFIG, help='private local YAML configuration')
    commands = root.add_subparsers(dest='command')
    commands.add_parser('join', help='explicitly join the configured meeting')
    leave = commands.add_parser('leave', help='explicitly leave the saved bot meeting')
    leave.add_argument('--retry-uncertain', action='store_true', help='retry only after checking meeting UI')
    collect = commands.add_parser('collect', help='collect continuously; Ctrl-C stops polling only')
    collect.add_argument('--once', action='store_true', help='one successful page-budget cycle (may leave pending pages)')
    status = commands.add_parser('status', help='show safe session and backlog statistics')
    status.add_argument('--consumer', default='fake-qa')
    consume = commands.add_parser('consume', help='persist a batch in the local fake consumer and acknowledge')
    consume.add_argument('--consumer', default='fake-qa')
    consume.add_argument('--limit', type=int, default=100)
    reset = commands.add_parser('reset-session', help='reset an uncertain session after manually verifying bot absence')
    reset.add_argument('--confirmed-bot-left', action='store_true', required=True)
    resolve = commands.add_parser('resolve-conflict', help='explicitly accept a stored candidate after local review')
    resolve.add_argument('--delivery-id', required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    store = source = None
    try:
        config = load_config(args.config)
        store = Store(config.data_directory / 'source.sqlite3', max_payload_bytes=config.content_capacity_bytes)
        client = FeishuClient(config.credentials, timeout=config.timeout_seconds, request_seconds=config.request_seconds)
        manager = SessionManager(store, config, client)
        if args.command == 'join':
            manager.join()
            print('Bot session verified; product session saved locally (identifiers hidden).')
        elif args.command == 'leave':
            manager.leave(retry_uncertain=args.retry_uncertain)
            print('Bot absence confirmed; product session marked left.')
        elif args.command == 'reset-session':
            manager.reset_uncertain(confirmed_bot_left=args.confirmed_bot_left)
            print('Uncertain session reset using your explicit confirmation.')
        elif args.command == 'collect':
            source = MeetingSource(store, manager)
            previous = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.getsignal(signum)
                    signal.signal(signum, lambda *_: source.stop_event.set())
            try:
                source.start(max_successful_cycles=1 if args.once else None)
                print('Collecting to private product storage; stopping does not leave the meeting.', file=sys.stderr)
                while source.running:
                    source.thread.join(timeout=0.2)
                status = source.state()
                print(json.dumps({'state': status.state.value, 'reason': status.reason}))
                if status.state == SourceState.ACTION_REQUIRED:
                    return 1
            finally:
                source.stop()
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
        elif args.command == 'status':
            session = manager.session()
            result = {'session_phase': session['phase'] if session else 'none'}
            if session and session.get('meeting_id'):
                mid = session['meeting_id']
                status = store.current_state(mid)
                result.update(state=status.state.value if status else 'not_started', reason=status.reason if status else 'not_started',
                              pending=store.backlog(mid, args.consumer))
            print(json.dumps(result))
        elif args.command == 'consume':
            session = manager.saved_meeting()
            rows = store.read(session['meeting_id'], args.consumer, limit=args.limit)
            consumer = DurableFakeConsumer(config.data_directory / 'fake_consumers' / (digest(args.consumer) + '.sqlite3'))
            try:
                accepted = sum(consumer.accept(row) for row in rows)
                if rows:
                    store.acknowledge(session['meeting_id'], args.consumer, rows[-1].sequence)
            finally:
                consumer.close()
            print(json.dumps({'offered': len(rows), 'newly_persisted': accepted, 'acknowledged': bool(rows)}))
        elif args.command == 'resolve-conflict':
            with manager.application_lock():
                session = manager.saved_meeting()
                changed = store.resolve_conflict(session['meeting_id'], args.delivery_id, at=utc_now())
            print(json.dumps({'resolved': changed}))
        return 0
    except (ConfigError, StorageError, LifecycleError, ApiError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print('Operation stopped: local file/database error; check private storage and free space.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, StopRequested):
        print('Operation interrupted; check saved session before retrying a meeting operation.', file=sys.stderr)
        return 130
    finally:
        if source and source.running:
            source.stop()
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
