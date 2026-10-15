"""Explicit local CLI. Credentials and meeting number are read only from YAML."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time

from . import __version__
from .adviser import AdviserService
from .client import ApiError, FeishuClient, StopRequested
from .collector import utc_now
from .config import ConfigError, DEFAULT_CONFIG, load_config
from .infrastructure.errors import LifecycleError, LockBusy
from .infrastructure.file_lock import FileLock
from .lifecycle import MeetingSource, SessionManager
from .diagnostics import Diagnostics
from .maintenance import Maintenance
from .models import SourceState, digest
from .llm import ModelError
from .rules import deadline_reply
from .reply_sender import introduce
from .storage import StorageError
from .adapters.persistence.factory import StorageFactory
from .cli.bootstrap import storage_plan


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
    collect.add_argument('--show-text', action='store_true', help='print new captions with speaker and timestamps locally after commit')
    status = commands.add_parser('status', help='show safe session and backlog statistics')
    status.add_argument('--consumer')
    consume = commands.add_parser('consume', help='consume with the configured model backend or the fixed test rule')
    consume.add_argument('--consumer')
    consume.add_argument('--limit', type=int, default=100)
    consume.add_argument('--backend', choices=('codex','opencode','rule'), help='override llm.backend for this invocation')
    consume.add_argument('--watch', action='store_true', help='continuously consume and decide; Ctrl-C stops the consumer only')
    reset = commands.add_parser('reset-session', help='reset stale/uncertain session after manually verifying bot absence')
    reset.add_argument('--confirmed-bot-left', action='store_true', required=True)
    resolve = commands.add_parser('resolve-conflict', help='explicitly accept a stored candidate after local review')
    resolve.add_argument('--delivery-id', required=True)
    commands.add_parser('prune', help='remove acknowledged expired records; stop collector first')
    delete = commands.add_parser('delete-meeting-data', help='irreversibly delete saved meeting content and consumer copies')
    delete.add_argument('--confirm', action='store_true', required=True)
    inspect = commands.add_parser('conflicts', help='list unresolved candidate hashes; text requires explicit flag')
    inspect.add_argument('--show-text', action='store_true')
    replies = commands.add_parser('replies', help='inspect saved model reply states; text requires explicit flag')
    replies.add_argument('--show-text', action='store_true')
    resolve_reply = commands.add_parser('resolve-reply', help='resolve one uncertain/rejected model send after checking meeting UI; stop consume first')
    resolve_reply.add_argument('--uuid', required=True)
    resolve_reply.add_argument('--outcome', choices=('sent','retry','cancel'), required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    store = source = adviser = None
    try:
        config = load_config(args.config)
        if args.command == 'consume' and args.backend:
            config = replace(config,llm=replace(config.llm,backend=args.backend))
        if args.command in ('status','consume'):
            args.consumer = args.consumer or ('adviser' if config.llm.backend in ('codex','opencode') else 'fake-qa')
        storage_factory = StorageFactory(config)
        plan = storage_plan(args.command)
        if plan.source_readonly and not storage_factory.source_path.exists():
            if args.command == 'status':
                print(json.dumps({'session_phase':'none'}))
                return 0
            raise StorageError('no saved product database for read-only command')
        store = storage_factory.source(readonly=plan.source_readonly)
        diagnostics = Diagnostics(config.data_directory / 'diagnostics', max_bytes=config.diagnostic_bytes,
                                  backups=config.diagnostic_backups)
        maintenance = Maintenance(store, config, diagnose=diagnostics.write)
        client = FeishuClient(config.credentials, timeout=config.timeout_seconds, request_seconds=config.request_seconds)
        manager = SessionManager(store, config, client)
        if args.command == 'join':
            manager.join()
            if config.llm.backend in ('codex','opencode') and config.llm.introduction:
                with FileLock(config.data_directory / 'locks' / 'adviser.lock'),maintenance.lock():
                    data = storage_factory.adviser()
                    try:
                        data.recover_sends()
                        if not introduce(data,manager,clock=time.time,diagnose=diagnostics.write):
                            raise LifecycleError('bot joined, but introduction send not confirmed; check meeting UI and replies before consuming')
                    finally:
                        data.close()
            print('Bot session verified; product session saved locally (identifiers hidden).')
        elif args.command == 'leave':
            manager.leave(retry_uncertain=args.retry_uncertain)
            print('Bot absence confirmed; product session marked left.')
        elif args.command == 'reset-session':
            manager.reset_uncertain(confirmed_bot_left=args.confirmed_bot_left)
            print('Uncertain session reset using your explicit confirmation.')
        elif args.command == 'collect':
            def print_caption(kind, caption):
                print(json.dumps({'kind': kind.value, 'timestamp': caption.received_at,
                                  'speaker': caption.speaker_name or '未知说话人',
                                  'event_time': caption.event_time, 'start_time': caption.start_time,
                                  'end_time': caption.end_time, 'text': caption.text,
                                  'mode': caption.mode.value}, ensure_ascii=False), flush=True)
            source = MeetingSource(store, manager, diagnose=diagnostics.write,
                                   on_caption=print_caption if args.show_text else None)
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
                result.update(store.statistics(mid, args.consumer))
                result['content_deleted'] = store.is_deleted(mid)
                path = config.data_directory / 'adviser.sqlite3'
                if path.exists():
                    lock = FileLock(config.data_directory / 'locks' / 'adviser.lock')
                    try:
                        lock.acquire()
                    except LockBusy:
                        active = True
                    else:
                        active = False
                        lock.release()
                    data = storage_factory.adviser(readonly=plan.adviser_readonly)
                    try:
                        result['adviser'] = {'backend': config.llm.backend} | data.status(mid,active=active)
                    finally:
                        data.close()
            print(json.dumps(result))
        elif args.command == 'consume' and config.llm.backend in ('codex','opencode'):
            adviser = AdviserService(
                store, manager, consumer_id=args.consumer, limit=args.limit,
                diagnose=diagnostics.write)
            previous = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT,signal.SIGTERM):
                    previous[signum] = signal.getsignal(signum)
                    signal.signal(signum,lambda *_: adviser.stop_event.set())
            try:
                print(f'Consuming with {config.llm.backend}; stopping does not stop collect or leave the meeting.',file=sys.stderr)
                summary = adviser.run(watch=args.watch,notify=(lambda value: print(json.dumps(value),flush=True)) if args.watch else None)
                if not args.watch:
                    print(json.dumps(summary))
                if summary['model_error'] or summary['uncertain_replies'] or summary['rejected_replies']:
                    return 1
            finally:
                adviser.close()
                adviser = None
                for signum,handler in previous.items():
                    signal.signal(signum,handler)
        elif args.command == 'consume':
            if args.watch:
                raise ConfigError('consume --watch requires llm.backend or --backend codex/opencode')
            session = manager.saved_meeting()
            with maintenance.lock():
                rows = store.read(session['meeting_id'], args.consumer, limit=args.limit)
                consumer = storage_factory.fake_consumer(digest(args.consumer))
                try:
                    accepted = replied = 0
                    for row in rows:
                        reply = deadline_reply(row)
                        accepted += consumer.accept(row, reply=reply)
                        pending = consumer.pending_reply(reply[0]) if reply else None
                        if pending:
                            manager.saved_meeting(require_joined=True)
                            try:
                                client.send_message(pending['meeting_id'], pending['content'], reply[0])
                            except (ApiError, StopRequested):
                                print('Reply not confirmed; input remains unacknowledged. Check meeting UI before rerunning consume; the same UUID will be reused.', file=sys.stderr)
                                raise
                            consumer.mark_reply_sent(reply[0])
                            replied += 1
                        store.acknowledge(session['meeting_id'], args.consumer, row.sequence)
                finally:
                    consumer.close()
            print(json.dumps({'offered': len(rows), 'newly_persisted': accepted, 'replied': replied, 'acknowledged': bool(rows)}))
        elif args.command in ('replies','resolve-reply'):
            session = manager.saved_meeting()
            path = config.data_directory / 'adviser.sqlite3'
            if not path.exists():
                if args.command == 'replies':
                    print('[]')
                else:
                    raise LifecycleError('no saved model replies')
            else:
                lock = FileLock(config.data_directory / 'locks' / 'adviser.lock')
                if args.command == 'resolve-reply':
                    lock.acquire()
                try:
                    data = storage_factory.adviser(readonly=plan.adviser_readonly)
                    try:
                        if args.command == 'replies':
                            print(json.dumps(data.list_replies(session['meeting_id'],show_text=args.show_text),ensure_ascii=False))
                        else:
                            data.resolve_reply(session['meeting_id'],args.uuid,args.outcome,now=time.time())
                            print(json.dumps({'resolved':True,'outcome':args.outcome}))
                    finally:
                        data.close()
                finally:
                    if args.command == 'resolve-reply':
                        lock.release()
        elif args.command == 'resolve-conflict':
            with manager.application_lock():
                session = manager.saved_meeting()
                changed = store.resolve_conflict(session['meeting_id'], args.delivery_id, at=utc_now())
            print(json.dumps({'resolved': changed}))
        elif args.command == 'prune':
            with manager.application_lock():
                result = maintenance.prune()
            print(json.dumps(result))
        elif args.command == 'delete-meeting-data':
            with manager.application_lock():
                session = manager.saved_meeting()
                maintenance.delete_meeting(session['meeting_id'])
            print('Saved meeting content and managed consumer copies deleted; bot session retained for explicit leave.')
        elif args.command == 'conflicts':
            session = manager.saved_meeting()
            result = store.list_conflicts(session['meeting_id'], show_text=args.show_text)
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ConfigError, StorageError, LifecycleError, ApiError, ModelError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print('Operation stopped: local file/database error; check private storage and free space.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, StopRequested):
        print('Operation interrupted; check saved session before retrying a meeting operation.', file=sys.stderr)
        return 130
    finally:
        if adviser:
            adviser.close()
        if source and source.running:
            source.stop()
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
