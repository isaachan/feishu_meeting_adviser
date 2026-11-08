"""Independent command handlers over lazily constructed CLI dependencies."""
from dataclasses import dataclass, field
import json
import sys
import time

from ..adviser import AdviserService
from ..client import ApiError, StopRequested
from ..config import ConfigError, Configuration
from ..diagnostics import Diagnostics
from ..infrastructure.errors import LifecycleError
from ..infrastructure.file_lock import FileLock
from ..infrastructure.signals import stop_on_signals
from ..lifecycle import SessionManager
from ..maintenance import Maintenance
from ..models import digest
from ..rules import deadline_reply
from .bootstrap import CommandStoragePlan


@dataclass
class CommandContext:
    config: Configuration
    storage_factory: object
    storage_plan: CommandStoragePlan
    store: object
    client_factory: object
    _diagnostics: object = field(default=None, init=False)
    _maintenance: object = field(default=None, init=False)
    _client: object = field(default=None, init=False)
    _manager: object = field(default=None, init=False)

    @property
    def diagnostics(self):
        if self._diagnostics is None:
            self._diagnostics = Diagnostics(
                self.config.data_directory / 'diagnostics',
                max_bytes=self.config.diagnostic_bytes,
                backups=self.config.diagnostic_backups,
            )
        return self._diagnostics

    @property
    def maintenance(self):
        if self._maintenance is None:
            self._maintenance = Maintenance(
                self.store, self.config, diagnose=self.diagnostics.write)
        return self._maintenance

    @property
    def client(self):
        if self._client is None:
            self._client = self.client_factory(
                self.config.credentials,
                timeout=self.config.timeout_seconds,
                request_seconds=self.config.request_seconds,
            )
        return self._client

    @property
    def manager(self):
        if self._manager is None:
            self._manager = SessionManager(self.store, self.config, self.client)
        return self._manager


def handle_status(context, args):
    session = context.manager.session()
    result = {'session_phase': session['phase'] if session else 'none'}
    if session and session.get('meeting_id'):
        mid = session['meeting_id']
        result.update(context.store.statistics(mid, args.consumer))
        result['content_deleted'] = context.store.is_deleted(mid)
        path = context.config.data_directory / 'adviser.sqlite3'
        if path.exists():
            lock = FileLock(context.config.data_directory / 'locks' / 'adviser.lock')
            active = lock.is_locked()
            data = context.storage_factory.adviser(
                readonly=context.storage_plan.adviser_readonly)
            try:
                result['adviser'] = {
                    'backend': context.config.llm.backend,
                } | data.status(mid, active=active)
            finally:
                data.close()
    print(json.dumps(result))
    return 0


def _consume_model(context, args):
    context.storage_factory.initialize_adviser()
    adviser = AdviserService(
        context.store,
        context.manager,
        consumer_id=args.consumer,
        limit=args.limit,
        diagnose=context.diagnostics.write,
        preview_display=lambda value: print(
            json.dumps(value, ensure_ascii=False), flush=True),
    )
    try:
        with stop_on_signals(adviser.stop_event.set):
            print(f'Consuming with {context.config.llm.backend}; stopping does not stop '
                  'collect or leave the meeting.', file=sys.stderr)
            summary = adviser.run(
                watch=args.watch,
                notify=(lambda value: print(json.dumps(value), flush=True))
                if args.watch else None,
            )
            if not args.watch:
                print(json.dumps(summary))
            return int(bool(summary['model_error'] or summary['uncertain_replies']
                            or summary['rejected_replies']))
    finally:
        adviser.close()


def _consume_rule(context, args):
    if args.watch:
        raise ConfigError(
            'consume --watch requires llm.backend or --backend codex/opencode')
    session = context.manager.saved_meeting()
    with context.maintenance.lock():
        rows = context.store.read(session['meeting_id'], args.consumer, limit=args.limit)
        consumer = context.storage_factory.fake_consumer(digest(args.consumer))
        try:
            accepted = replied = 0
            for row in rows:
                reply = deadline_reply(row)
                accepted += consumer.accept(row, reply=reply)
                pending = consumer.pending_reply(reply[0]) if reply else None
                if pending:
                    context.manager.saved_meeting(require_joined=True)
                    try:
                        context.client.send_message(
                            pending['meeting_id'], pending['content'], reply[0])
                    except (ApiError, StopRequested):
                        print('Reply not confirmed; input remains unacknowledged. Check '
                              'meeting UI before rerunning consume; the same UUID will be reused.',
                              file=sys.stderr)
                        raise
                    consumer.mark_reply_sent(reply[0])
                    replied += 1
                context.store.acknowledge(
                    session['meeting_id'], args.consumer, row.sequence)
        finally:
            consumer.close()
    print(json.dumps({'offered': len(rows), 'newly_persisted': accepted,
                      'replied': replied, 'acknowledged': bool(rows)}))
    return 0


def handle_consume(context, args):
    if context.config.llm.backend in ('codex', 'opencode'):
        return _consume_model(context, args)
    return _consume_rule(context, args)


def handle_replies(context, args):
    session = context.manager.saved_meeting()
    path = context.config.data_directory / 'adviser.sqlite3'
    if not path.exists():
        print('[]')
        return 0
    data = context.storage_factory.adviser(
        readonly=context.storage_plan.adviser_readonly)
    try:
        print(json.dumps(data.list_replies(
            session['meeting_id'], show_text=args.show_text), ensure_ascii=False))
    finally:
        data.close()
    return 0


def handle_resolve_reply(context, args):
    session = context.manager.saved_meeting()
    path = context.config.data_directory / 'adviser.sqlite3'
    if not path.exists():
        raise LifecycleError('no saved model replies')
    lock = FileLock(context.config.data_directory / 'locks' / 'adviser.lock')
    lock.acquire()
    try:
        data = context.storage_factory.adviser(
            readonly=context.storage_plan.adviser_readonly)
        try:
            data.resolve_reply(session['meeting_id'], args.uuid, args.outcome, now=time.time())
            print(json.dumps({'resolved': True, 'outcome': args.outcome}))
        finally:
            data.close()
    finally:
        lock.release()
    return 0


COMMAND_HANDLERS = {
    'status': handle_status,
    'consume': handle_consume,
    'replies': handle_replies,
    'resolve-reply': handle_resolve_reply,
}
