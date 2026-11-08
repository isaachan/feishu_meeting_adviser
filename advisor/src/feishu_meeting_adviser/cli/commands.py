"""Independent command handlers over lazily constructed CLI dependencies."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import time

from ..adviser import AdviserService
from ..config import ConfigError, Configuration
from ..diagnostics import Diagnostics
from ..infrastructure.errors import LifecycleError
from ..infrastructure.file_lock import FileLock
from ..infrastructure.signals import stop_on_signals
from ..local_sessions import LocalSessionManager
from ..maintenance import Maintenance
from ..models import digest
from ..storage import StorageError
from .bootstrap import CommandStoragePlan


@dataclass
class CommandContext:
    config: Configuration
    storage_factory: object
    storage_plan: CommandStoragePlan
    store: object | None
    data: object | None
    args: object
    _diagnostics: object = field(default=None, init=False)

    @property
    def diagnostics(self):
        if self._diagnostics is None:
            self._diagnostics = Diagnostics(
                self.config.data_directory / 'diagnostics',
                max_bytes=self.config.diagnostic_bytes,
                backups=self.config.diagnostic_backups,
            )
        return self._diagnostics

    def session(self):
        if self.data is None:
            raise StorageError('no saved adviser database; create a session first')
        return self.data.require_local_session(self.args.session)

    def lease_lock(self, meeting_id):
        return FileLock(
            self.config.data_directory / 'locks' / ('local-' + digest(meeting_id) + '.lock'))


def _read_description(args):
    if not args.description_file:
        return ''
    path = Path(args.description_file).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ConfigError('description file must be a regular local file')
    raw = path.read_bytes()
    if len(raw) > 200_000:
        raise ConfigError('description file exceeds 200000 bytes')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ConfigError('description file must be UTF-8 encoded') from None


def handle_session_new(context, args):
    session = context.data.create_local_session(
        title=args.title or '', description=_read_description(args))
    context.store.ensure_meeting(session['meeting_id'], {})
    print(json.dumps({'session_id': session['meeting_id'], 'phase': session['phase'],
                      'title': session['title'], 'started_at': session['started_at']},
                     ensure_ascii=False))
    return 0


def handle_session_list(context, args):
    sessions = context.data.list_local_sessions() if context.data else []
    print(json.dumps([{'session_id': session['meeting_id'], 'title': session['title'],
                       'phase': session['phase'], 'started_at': session['started_at']}
                      for session in sessions], ensure_ascii=False))
    return 0


def _session_idle(context, session):
    if context.lease_lock(session['meeting_id']).is_locked():
        return False
    consumer = getattr(context.args, 'consumer', None) or 'adviser'
    if context.store is not None and context.store.backlog(session['meeting_id'], consumer) > 0:
        return False
    meeting = context.data.meeting(session['meeting_id'])
    if meeting is None:
        return True
    if meeting['generation'] > meeting['decided_generation']:
        return False
    return context.data.next_reply(session['meeting_id'], now=time.time()) is None


def handle_session_end(context, args):
    session = context.session()
    target = 'ended' if args.force or _session_idle(context, session) else 'draining'
    updated = context.data.set_local_session_phase(session['meeting_id'], target)
    print(json.dumps({'session_id': updated['meeting_id'], 'phase': updated['phase']}))
    return 0


def handle_status(context, args):
    session = context.session()
    mid = session['meeting_id']
    result = {'session_phase': session['phase'], 'session_source': 'local'}
    result.update(context.store.statistics(mid, args.consumer))
    result['content_deleted'] = context.store.is_deleted(mid)
    if context.data is not None:
        active = context.lease_lock(mid).is_locked()
        result['adviser'] = {
            'backend': context.config.llm.backend,
        } | context.data.status(mid, active=active)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def handle_consume(context, args):
    if context.config.llm.backend not in ('codex', 'opencode'):
        raise ConfigError('local sessions require llm.backend codex/opencode or --backend')
    session = context.session()
    manager = LocalSessionManager(context.data, context.config, session['meeting_id'])
    adviser = AdviserService(
        context.store,
        manager,
        consumer_id=args.consumer,
        limit=args.limit,
        diagnose=context.diagnostics.write,
        preview_display=lambda value: print(
            json.dumps(value, ensure_ascii=False), flush=True),
    )
    try:
        with stop_on_signals(adviser.stop_event.set):
            print(f'Consuming local session with {context.config.llm.backend}; '
                  'stopping does not delete the session.', file=sys.stderr)
            summary = adviser.run(
                watch=args.watch,
                notify=(lambda value: print(json.dumps(value), flush=True))
                if args.watch else None,
            )
            if not args.watch:
                print(json.dumps(summary, ensure_ascii=False))
            return int(bool(summary['model_error'] or summary['uncertain_replies']
                            or summary['rejected_replies']))
    finally:
        adviser.close()


def handle_replies(context, args):
    session = context.session()
    rows = (context.data.list_replies(session['meeting_id'], show_text=args.show_text)
            if context.data else [])
    print(json.dumps(rows, ensure_ascii=False))
    return 0


def handle_resolve_reply(context, args):
    session = context.session()
    if context.data is None:
        raise LifecycleError('no saved model replies')
    lock = context.lease_lock(session['meeting_id'])
    lock.acquire()
    try:
        context.data.resolve_reply(
            session['meeting_id'], args.uuid, args.outcome, now=time.time())
        print(json.dumps({'resolved': True, 'outcome': args.outcome}))
    finally:
        lock.release()
    return 0


COMMAND_HANDLERS = {
    'session-new': handle_session_new,
    'session-list': handle_session_list,
    'session-end': handle_session_end,
    'status': handle_status,
    'consume': handle_consume,
    'replies': handle_replies,
    'resolve-reply': handle_resolve_reply,
}
