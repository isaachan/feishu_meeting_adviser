"""Independent command handlers over lazily constructed CLI dependencies."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import time

from ..client import ApiError, StopRequested
from ..collector import utc_now
from ..config import ConfigError, Configuration, parse_meeting_reference
from ..diagnostics import Diagnostics
from ..infrastructure.errors import LifecycleError
from ..infrastructure.file_lock import FileLock
from ..infrastructure.signals import stop_on_signals
from ..lifecycle import MeetingSource, SessionManager
from ..local_source import LocalCaptionSource
from ..maintenance import Maintenance
from ..models import SourceState, digest
from ..reply_sender import introduce
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


def handle_join(context, args):
    meeting_no, link_password = parse_meeting_reference(args.meeting)
    context.manager.join(meeting_no, args.password or link_password)
    config = context.config
    if (config.llm.backend in ('codex', 'opencode') and config.llm.introduction
            and config.notifications.meeting_comment):
        with (FileLock(config.data_directory / 'locks' / 'adviser.lock'),
              context.maintenance.lock()):
            context.storage_factory.initialize_adviser()
            data = context.storage_factory.adviser()
            try:
                data.recover_sends()
                if not introduce(data, context.manager, clock=time.time,
                                 diagnose=context.diagnostics.write):
                    raise LifecycleError(
                        'bot joined, but introduction send not confirmed; '
                        'check meeting UI and replies before consuming')
            finally:
                data.close()
    print('Bot session verified; product session saved locally (identifiers hidden).')
    return 0


def handle_leave(context, args):
    context.manager.leave(retry_uncertain=args.retry_uncertain)
    print('Bot absence confirmed; product session marked left.')
    return 0


def handle_reset_session(context, args):
    context.manager.reset_uncertain(confirmed_bot_left=args.confirmed_bot_left)
    print('Uncertain session reset using your explicit confirmation.')
    return 0


def _print_caption(kind, caption):
    print(json.dumps({
        'kind': kind.value,
        'timestamp': caption.received_at,
        'speaker': caption.speaker_name or '未知说话人',
        'event_time': caption.event_time,
        'start_time': caption.start_time,
        'end_time': caption.end_time,
        'text': caption.text,
        'mode': caption.mode.value,
    }, ensure_ascii=False), flush=True)


def handle_collect(context, args):
    if getattr(args, 'captions', None):
        return _handle_collect_local(context, args)
    source = MeetingSource(
        context.store,
        context.manager,
        diagnose=context.diagnostics.write,
        on_caption=_print_caption if args.show_text else None,
    )
    try:
        with stop_on_signals(source.stop_event.set):
            source.start(max_successful_cycles=1 if args.once else None)
            print('Collecting to private product storage; stopping does not leave the meeting.',
                  file=sys.stderr)
            while source.running:
                source.thread.join(timeout=0.2)
            status = source.state()
            print(json.dumps({'state': status.state.value, 'reason': status.reason}))
            return 1 if status.state == SourceState.ACTION_REQUIRED else 0
    finally:
        source.stop()


def _handle_collect_local(context, args):
    if args.once:
        raise ConfigError('--once is not supported together with --captions')
    if args.captions == '-':
        stream = sys.stdin
    else:
        stream = Path(args.captions).expanduser().open('r', encoding='utf-8')
    source = LocalCaptionSource(
        context.store,
        context.manager,
        speaker=args.speaker,
        diagnose=context.diagnostics.write,
        on_caption=_print_caption if args.show_text else None,
        stream=stream,
    )
    try:
        print('Collecting local caption input to private product storage; Ctrl-C stops collection only, '
              'not the meeting.', file=sys.stderr)
        status = source.run()
        print(json.dumps({'state': status.state.value, 'reason': status.reason}))
        return 1 if status.state == SourceState.ACTION_REQUIRED else 0
    finally:
        if stream is not sys.stdin:
            stream.close()


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


def handle_resolve_conflict(context, args):
    with context.manager.application_lock():
        session = context.manager.saved_meeting()
        changed = context.store.resolve_conflict(
            session['meeting_id'], args.delivery_id, at=utc_now())
    print(json.dumps({'resolved': changed}))
    return 0


def handle_prune(context, args):
    with context.manager.application_lock():
        result = context.maintenance.prune()
    print(json.dumps(result))
    return 0


def handle_delete_meeting_data(context, args):
    with context.manager.application_lock():
        session = context.manager.saved_meeting()
        context.maintenance.delete_meeting(session['meeting_id'])
    print('Saved meeting content and managed consumer copies deleted; bot session retained '
          'for explicit leave.')
    return 0


def handle_conflicts(context, args):
    session = context.manager.saved_meeting()
    result = context.store.list_conflicts(
        session['meeting_id'], show_text=args.show_text)
    print(json.dumps(result, ensure_ascii=False))
    return 0


COMMAND_HANDLERS = {
    'join': handle_join,
    'leave': handle_leave,
    'collect': handle_collect,
    'status': handle_status,
    'reset-session': handle_reset_session,
    'resolve-conflict': handle_resolve_conflict,
    'prune': handle_prune,
    'delete-meeting-data': handle_delete_meeting_data,
    'conflicts': handle_conflicts,
}
