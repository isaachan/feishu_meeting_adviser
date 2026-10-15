"""Verified bot sessions, operation locks and a stoppable transcript source."""
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from uuid import uuid4

from .client import ApiError, ErrorKind, FeishuClient, StopRequested
from .collector import Collector, utc_now
from .config import ConfigError, Configuration, file_fingerprint
from .models import SourceState, Status, digest
from .recovery import RecoveryLoop
from .storage import StorageError, Store
from .infrastructure.errors import LifecycleError, LockBusy
from .infrastructure.file_lock import FileLock


class SessionManager:
    def __init__(self, store: Store, config: Configuration, client, *, clock=time.time):
        self.store = store
        self.config = config
        self.client = client
        self.clock = clock
        self.app_key = digest(config.credentials.app_id)

    def application_lock(self):
        return FileLock(self.config.data_directory / 'locks' / ('app_' + self.app_key + '.lock'))

    def session(self, store=None):
        return (store or self.store).session(self.app_key)

    def saved_meeting(self, *, require_joined=False) -> dict:
        session = self.session()
        if not session or not session.get('meeting_id'):
            raise LifecycleError('no verified product meeting session; run join first')
        if require_joined and (session['phase'] != 'joined' or session['meeting_no'] != self.config.meeting_no):
            raise LifecycleError('saved bot is not joined to configured meeting; check session before proceeding')
        return session

    def _bot_absent(self, session) -> bool:
        # Existing events permission suffices; do not scan/save old transcript data.
        now = str(int(self.clock()))
        try:
            self.client.events({'meeting_id': session['meeting_id'], 'start_time': now,
                                'end_time': now, 'page_size': 20})
        except ApiError as exc:
            if exc.code == 120004:  # Events contract: application bot is not in this meeting.
                return True
            raise
        return False  # An empty page is not proof that the bot left or the meeting ended.

    def _save_left(self, session, reason):
        self.store.save_session(self.app_key, {**session, 'phase': 'left',
            'left_at': utc_now(self.clock), 'leave_reason': reason})

    def join(self) -> dict:
        with self.application_lock(), FileLock(self.config.data_directory / 'locks' / 'adviser.lock'):
            current = self.session()
            if current and current['phase'] == 'joined':
                if self._bot_absent(current):
                    self._save_left(current, 'bot_absence_verified')
                elif current['meeting_no'] == self.config.meeting_no:
                    return current
                else:
                    raise LifecycleError('bot is still in another meeting; run leave before joining the configured meeting')
                current = self.session()
            if current and current['phase'] == 'leave_uncertain' and current.get('meeting_id'):
                if self._bot_absent(current):
                    self._save_left(current, 'bot_absence_verified')
                    current = self.session()
            if current and current['phase'] in ('join_pending', 'join_uncertain', 'leave_uncertain'):
                raise LifecycleError('bot session outcome uncertain; check meeting UI before retrying')
            pending = {'phase': 'join_pending', 'meeting_no': self.config.meeting_no, 'at': utc_now(self.clock),
                       'join_instance':str(uuid4())}
            self.store.save_session(self.app_key, pending)
            try:
                result = self.client.join(self.config.meeting_no, self.config.password)
                data = result.get('data') if isinstance(result, dict) else None
                meeting = data.get('meeting') if isinstance(data, dict) else None
                if not isinstance(meeting, dict) or meeting.get('meeting_no') != self.config.meeting_no:
                    raise LifecycleError('join response meeting mismatch; outcome uncertain, check meeting UI')
                mid = meeting.get('id')
                if not isinstance(mid, str) or not mid.isascii() or not mid.isdecimal() or len(mid) <= 9:
                    raise LifecycleError('join response missing internal meeting ID; outcome uncertain')
                user = data.get('join_user')
                bot_id = user.get('id') if isinstance(user, dict) else None
                joined = {**pending, 'phase': 'joined', 'meeting_id': mid, 'bot_user_id': bot_id if isinstance(bot_id, str) else None,
                          'joined_at': int(self.clock())}
                self.store.save_session(self.app_key, joined)
                return joined
            except ApiError as exc:
                uncertain = exc.kind in (ErrorKind.NETWORK, ErrorKind.SERVER, ErrorKind.RESPONSE, ErrorKind.ACTION_REQUIRED)
                self.store.save_session(self.app_key, {**pending, 'phase': 'join_uncertain' if uncertain else 'join_rejected'})
                raise
            except (LifecycleError, StopRequested):
                self.store.save_session(self.app_key, {**pending, 'phase': 'join_uncertain'})
                raise

    def leave(self, *, retry_uncertain=False) -> None:
        with self.application_lock():
            session = self.session()
            if not session:
                raise LifecycleError('no verified bot session; absence is not confirmed')
            if session['phase'] == 'left':
                return
            if session['phase'] == 'leave_uncertain' and not retry_uncertain:
                raise LifecycleError('leave outcome uncertain; check meeting UI, then explicitly retry if needed')
            if session['phase'] not in ('joined', 'leave_uncertain') or not session.get('meeting_id'):
                raise LifecycleError('no verified meeting ID for leaving; check meeting UI')
            self.store.save_session(self.app_key, {**session, 'phase': 'leave_uncertain'})
            try:
                self.client.leave(session['meeting_id'])
            except ApiError as exc:
                if exc.code == 121104:
                    # Meeting-state rejection alone is ambiguous. Reconcile membership
                    # using the same permission already used by the collector.
                    self.store.save_session(self.app_key, session)
                    if self._bot_absent(session):
                        self._save_left(session, 'bot_absence_verified')
                        return
                    raise LifecycleError('meeting status does not support leaving (code=121104); '
                                         'bot absence is not confirmed; check meeting UI before using '
                                         'reset-session --confirmed-bot-left') from None
                if exc.code != 121110:
                    if exc.kind not in (ErrorKind.NETWORK, ErrorKind.SERVER, ErrorKind.RESPONSE, ErrorKind.ACTION_REQUIRED):
                        self.store.save_session(self.app_key, session)
                    raise
                self._save_left(session, 'bot_absence_verified')
                return
            self._save_left(session, 'leave_confirmed')

    def reset_uncertain(self, *, confirmed_bot_left: bool) -> None:
        if not confirmed_bot_left:
            raise LifecycleError('reset requires an explicit confirmation that the bot is out of the meeting')
        with self.application_lock():
            current = self.session()
            if not current or current['phase'] not in ('join_pending', 'join_uncertain', 'leave_uncertain', 'joined'):
                raise LifecycleError('no active or uncertain session to reset')
            self.store.save_session(self.app_key, {**current, 'phase': 'left', 'manual_confirmation_at': utc_now(self.clock)})


class MeetingSource:
    """One worker connection; caller reads/acks using its own SQLite connection."""
    def __init__(self, store: Store, manager: SessionManager, client=None, *, clock=time.time, diagnose=None, on_caption=None):
        self.store = store
        self._owner_thread = threading.get_ident()
        self.manager = manager
        self.config = manager.config
        self.clock = clock
        self.session = manager.saved_meeting(require_joined=True)
        self.meeting_id = self.session['meeting_id']
        self.stop_event = threading.Event()
        self.client = client or FeishuClient(self.config.credentials, timeout=self.config.timeout_seconds,
                                             request_seconds=self.config.request_seconds, wait=self.stop_event.wait)
        self.diagnose = diagnose or (lambda record: None)
        self.on_caption = on_caption
        self.thread = None
        self.failure = None
        self.failure_reason = None
        self._app_lock = manager.application_lock()
        self._meeting_lock = FileLock(self.config.data_directory / 'locks' / ('meeting_' + digest(self.meeting_id) + '.lock'))

    def _before_cycle(self, worker_store):
        session = self.manager.session(worker_store)
        if session != self.session:
            return 'session_changed'
        try:
            if file_fingerprint(self.config.path) != self.config.fingerprint:
                return 'configuration_changed'
        except (ConfigError, OSError):
            return 'configuration_unavailable'
        return None

    def _page_status(self, page):
        bot_id = self.session.get('bot_user_id')
        for uid, departed_ns in page.departures:
            if uid == bot_id and (departed_ns is None or departed_ns >= self.session['joined_at'] * 1_000_000_000):
                return Status(self.meeting_id, SourceState.ACTION_REQUIRED,
                              'bot_left_event' if departed_ns is not None else 'bot_membership_uncertain', utc_now(self.clock))
        return None

    def start(self, *, max_successful_cycles=None) -> None:
        if threading.get_ident() != self._owner_thread:
            raise LifecycleError('start source on its creating thread; downstream reads may use other threads')
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.failure = None
        self.failure_reason = None
        self._app_lock.acquire()
        try:
            self._meeting_lock.acquire()
            collector = Collector(self.store, self.meeting_id, self.client, settings=self.config.scan, clock=self.clock)
            cp = self.store.checkpoint(self.meeting_id)
            if cp['last_success'] is not None:
                cp['recovery_start'] = cp['completed_until'] or cp['query_start']
                cp['mode'] = 'recovery_backfill'
                self.store.commit_page(self.meeting_id, [], cp)
            status = (Status(self.meeting_id, SourceState.RECOVERING, 'resume_pending', utc_now(self.clock), cp.get('recovery_start'))
                      if cp.get('recovery_start') is not None else self.store.active_status(self.meeting_id, utc_now(self.clock)))
            self.store.publish_status(status)
            self.thread = threading.Thread(target=self._worker, kwargs={'max_successful_cycles': max_successful_cycles},
                                           name='meeting-transcript-source', daemon=False)
            self.thread.start()
        except BaseException:
            self._meeting_lock.release()
            self._app_lock.release()
            raise

    def _worker(self, *, max_successful_cycles):
        worker_store = None
        loop = None
        try:
            worker_store = Store(self.store.path, max_payload_bytes=self.store.max_payload_bytes,
                                 max_database_bytes=self.store.max_database_bytes)
            from .maintenance import Maintenance
            maintenance = Maintenance(worker_store, self.config, clock=self.clock, diagnose=self.diagnose)
            def before_cycle():
                reason = self._before_cycle(worker_store)
                if reason is None:
                    maintenance.run_due()
                return reason
            collector = Collector(worker_store, self.meeting_id, self.client, settings=self.config.scan,
                                  clock=self.clock, should_stop=self.stop_event.is_set, page_status=self._page_status,
                                  diagnose=self.diagnose, on_caption=self.on_caption)
            loop = RecoveryLoop(collector, stop_event=self.stop_event, poll_seconds=self.config.poll_seconds,
                                cooldown_seconds=self.config.cooldown_seconds, diagnose=self.diagnose,
                                before_cycle=before_cycle)
            loop.run(max_successful_cycles=max_successful_cycles)
            if not loop.terminal or self.stop_event.is_set():
                worker_store.publish_status(Status(self.meeting_id, SourceState.STOPPED, 'polling_stopped_bot_remains', utc_now(self.clock)))
        except Exception as exc:
            self.failure = type(exc).__name__
            self.failure_reason = 'local_storage' if isinstance(exc, (StorageError,OSError)) else 'source_worker_failed'
            try:
                self.diagnose({'kind': 'error', 'error_kind': self.failure_reason})
            except (StorageError, OSError):
                pass
            if worker_store:
                try:
                    worker_store.publish_status(Status(self.meeting_id, SourceState.ACTION_REQUIRED, self.failure_reason, utc_now(self.clock)))
                except (StorageError, OSError):
                    pass
        finally:
            if worker_store:
                worker_store.close()
            self._meeting_lock.release()
            self._app_lock.release()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=self.config.timeout_seconds + 6)
            if self.thread.is_alive():
                raise LifecycleError('collector still shutting down; do not close its storage yet')

    @contextmanager
    def _access_store(self):
        if threading.get_ident() == self._owner_thread:
            yield self.store
        else:
            connection = Store(self.store.path, max_payload_bytes=self.store.max_payload_bytes,
                               max_database_bytes=self.store.max_database_bytes)
            try:
                yield connection
            finally:
                connection.close()

    def read(self, consumer_id: str, *, limit=100):
        with self._access_store() as store:
            return store.read(self.meeting_id, consumer_id, limit=limit)

    def acknowledge(self, consumer_id: str, sequence: int):
        with self._access_store() as store:
            store.acknowledge(self.meeting_id, consumer_id, sequence)

    def state(self):
        with self._access_store() as store:
            status = store.current_state(self.meeting_id)
        if self.failure:
            return Status(self.meeting_id, SourceState.ACTION_REQUIRED, self.failure_reason, utc_now(self.clock))
        return status or Status(self.meeting_id, SourceState.STOPPED, 'not_started', utc_now(self.clock))
