"""Independent durable inbox and one bounded model worker per meeting consumer."""
from contextlib import contextmanager
import json
import queue
import threading
import time

from .adviser_store import AdviserStore
from .client import ApiError, StopRequested
from .config import file_fingerprint
from .diagnostics import sanitize
from .lifecycle import FileLock, LifecycleError, LockBusy
from .llm import CodexBackend, ModelError, load_role
from .maintenance import Maintenance
from .reply_sender import introduce, send_saved_reply
from .models import digest
from .materials import ContentPreviews, MATERIAL_POLICY, MaterialError, load_materials
from .oauth import OAuthError, OAuthManager
from .storage import StorageError


class AdviserRunner:
    def __init__(self, source, manager, *, backend=None, stop_event=None, clock=time.time, consumer_id='adviser', limit=100, diagnose=None,
                 material_client=None, preview_display=None):
        self.source, self.manager = source, manager
        self.config, self.settings = manager.config, manager.config.llm
        self.session = manager.saved_meeting(require_joined=True)
        self.mid = self.session['meeting_id']
        self.clock = clock
        self.consumer_id, self.limit = consumer_id, limit
        self.diagnose = diagnose or (lambda record:None)
        self.stop_event = stop_event or threading.Event()
        self.role, self.role_file_version = load_role(self.settings)
        self.role_version = self.role_file_version
        if self.config.meeting_context.enabled:
            self.role += MATERIAL_POLICY
            self.role_version = digest([self.role_file_version,MATERIAL_POLICY])
        self.data = AdviserStore(self.config.data_directory / 'adviser.sqlite3',
            max_database_bytes=self.config.database_capacity_bytes, max_payload_bytes=self.config.content_capacity_bytes)
        self.data.ensure_meeting(self.mid)
        self.maintenance = Maintenance(source, self.config)
        self.lock = FileLock(self.config.data_directory / 'locks' / 'adviser.lock')
        self.backend = backend or CodexBackend(self.settings, self.config.data_directory / 'model_runtime', stop_event=self.stop_event)
        self.worker = None
        self.results = queue.Queue(maxsize=1)
        self.batch_due = None
        self.offered = self.persisted = self.decisions = self.replied = 0
        self.model_failure = None
        self.material_loaded = False
        self.material_subject = None
        self.material_client, self.preview_display = material_client, preview_display
        self.material_oauth = getattr(material_client,'oauth_manager',None)

    def _ensure_materials(self):
        if self.material_loaded:
            return
        with self._maintenance_lock():
            self.guard()
            self.data.ensure_session(self.session)
            if not self.config.meeting_context.enabled:
                self.data.set_materials(self.mid,{'state':'disabled','context_version':None,'documents':[]},now=self.clock())
                self.material_loaded = True
                return
            self.data.db.execute('UPDATE meetings SET material_state=\'loading\' WHERE meeting_id=?',(self.mid,))
        settings = self.config.meeting_context
        logger = ContentPreviews(self.config.data_directory/'content_previews',self.mid,
            max_bytes=settings.preview_bytes,backups=settings.preview_backups,display=self.preview_display)
        try:
            snapshot = load_materials(self.config,self.session,client=self.material_client,
                emit=logger.write,stop_event=self.stop_event,clock=self.clock)
        except (ApiError,OAuthError,MaterialError,StorageError,OSError,ValueError,TypeError,KeyError) as exc:
            reason = exc.kind.value if isinstance(exc,ApiError) else exc.reason if isinstance(exc,OAuthError) else 'material_load_failed'
            with self._maintenance_lock():
                self.data.set_materials(self.mid,{'state':'unavailable','context_version':digest(['unavailable',settings.calendar_id,settings.event_id]),
                    'reason':reason,'documents':[]},now=self.clock())
            logger.write({'kind':'load','status':'unavailable','reason':reason,
                'http_status':getattr(exc,'http_status',None),'code':getattr(exc,'code',None)})
            raise MaterialError('meeting materials unavailable; collect continues independently; '+reason) from None
        with self._maintenance_lock():
            self.guard()
            self.data.set_materials(self.mid,snapshot,now=self.clock())
        self.material_subject = snapshot.get('subject_hash')
        self.material_loaded = True

    @contextmanager
    def _maintenance_lock(self):
        # Collector retention and consumer operations share this short-lived lock.
        # Contention is expected; only the separate adviser lease rejects concurrency.
        lock = self.maintenance.lock()
        while True:
            if self.stop_event.is_set():
                raise StopRequested()
            try:
                lock.acquire()
                break
            except LockBusy:
                self.stop_event.wait(0.05)
        try:
            if self.stop_event.is_set():
                raise StopRequested()
            yield
        finally:
            lock.release()

    def guard(self):
        if self.source.db.execute('SELECT 1 FROM tombstones WHERE identity=?',(digest(self.mid),)).fetchone():
            raise LifecycleError('meeting content was deleted; model consumption cannot restart for this meeting')
        if self.manager.session() != self.session:
            raise LifecycleError('adviser session changed; restart after checking meeting state')
        if file_fingerprint(self.config.path) != self.config.fingerprint:
            raise LifecycleError('adviser configuration changed; restart consumer')
        if load_role(self.settings)[1] != self.role_file_version:
            raise LifecycleError('adviser role changed; restart consumer')
        if self.material_loaded and self.material_subject is not None:
            try:
                oauth = self.material_oauth or OAuthManager(self.config.credentials,self.config.data_directory)
                subject = oauth.subject_hash()
            except OAuthError:
                raise MaterialError('material user authorization unavailable; restart after authorizing') from None
            if subject != self.material_subject:
                raise MaterialError('material user account changed; restart consume')
        state = self.source.current_state(self.mid)
        if state and state.state.value in ('action_required','ended','stopped'):
            raise LifecycleError('transcript source is unavailable or stopped; check status, restart collect, then restart consume')

    def ingest(self):
        with self._maintenance_lock():
            self.guard()
            rows = self.source.read(self.mid, self.consumer_id, limit=self.limit)
            inserted = self.data.ingest(self.mid, rows, now=self.clock())
            # If this ack is lost, replay is absorbed by the committed durable inbox.
            if rows:
                self.source.acknowledge(self.mid, self.consumer_id, rows[-1].sequence)
            self.offered += len(rows)
            self.persisted += inserted
            if inserted and self.batch_due is None:
                self.batch_due = self.clock() + self.settings.batch_seconds
            return inserted

    def _start_job(self, job):
        self.data.mark_job_running(job['job_id'])
        def work():
            started = time.monotonic()
            try:
                decision = self.backend.decide(json.loads(job['snapshot']), self.role)
                self.results.put((job, decision, None, (time.monotonic()-started)*1000))
            except ModelError as exc:
                self.results.put((job, None, exc.reason, (time.monotonic()-started)*1000))
            except Exception:
                self.results.put((job, None, 'model_failed', (time.monotonic()-started)*1000))
        self.worker = threading.Thread(target=work, name='meeting-model-decision', daemon=False)
        self.worker.start()

    def _finish_job(self, job, decision, elapsed):
        self.data.finish_job(job, decision, now=self.clock(), settings=self.settings,
                             joined_at=self.session['joined_at'], bot_id=self.session.get('bot_user_id'))
        self.decisions += 1
        self.diagnose(self._model_metadata() | {'kind':'model','backend':'codex','model_ms':elapsed,'decision_saved':True,
                       'participation':self.data.meeting(self.mid)['participation']})

    def _model_metadata(self):
        read = getattr(self.backend, 'diagnostic_metadata', None)
        metadata = read() if callable(read) else {}
        return sanitize(metadata | {'kind': 'model'}) if isinstance(metadata, dict) else {}

    def dispatch(self):
        if self.worker is not None or self.stop_event.is_set():
            return
        with self._maintenance_lock():
            self.guard()
            # Source may have committed captions after this step's ingest or model completion.
            if self.source.backlog(self.mid,self.consumer_id):
                return
            self.data.discard_expired_replies(self.mid,self.settings,now=self.clock(),
                joined_at=self.session['joined_at'],bot_id=self.session.get('bot_user_id'))
            row = self.data.next_reply(self.mid, now=self.clock())
            if row is None:
                return
            self.replied += int(send_saved_reply(self.data,self.manager.client,row,clock=self.clock,
                                interval=self.settings.reply_interval_seconds,diagnose=self.diagnose))

    def step(self):
        self._ensure_materials()
        self.ingest()
        with self._maintenance_lock():
            self.data.discard_expired_replies(self.mid,self.settings,now=self.clock(),
                joined_at=self.session['joined_at'],bot_id=self.session.get('bot_user_id'))
        if self.worker is not None and not self.worker.is_alive():
            self.worker.join()
            with self._maintenance_lock():
                self.guard()
                self.worker = None
                job, decision, error, elapsed = self.results.get_nowait()
                if error:
                    self.data.fail_job(job, error, retry_at=self.clock()+self.config.cooldown_seconds)
                    self.model_failure = error
                    self.diagnose(self._model_metadata() | {'kind':'model','error_kind':error,'model_ms':elapsed})
                else:
                    try:
                        self._finish_job(job, decision, elapsed)
                        self.model_failure = None
                    except ModelError as exc:
                        self.data.fail_job(job,exc.reason,retry_at=self.clock()+self.config.cooldown_seconds)
                        self.model_failure = exc.reason
        if self.worker is None and (self.batch_due is None or self.clock() >= self.batch_due):
            with self._maintenance_lock():
                self.guard()
                job = self.data.prepare_job(self.mid, self.settings, self.role_version,
                    now=self.clock(), joined_at=self.session['joined_at'], bot_id=self.session.get('bot_user_id'),
                    role_characters=len(self.role))
            if job:
                self._start_job(job)
            self.batch_due = None
        self.dispatch()
        return self.statistics()

    def statistics(self):
        meeting = self.data.meeting(self.mid)
        # Avoid changing elapsed counters on each watch tick and flooding stdout.
        stage = self._model_metadata().get('model_stage')
        live = {'model_stage': stage} if isinstance(stage, str) and stage in {
            'startup','initialize','account_read','mcp_discovery','thread_start',
            'mcp_verify','turn_start','generation','thread_cleanup'} else {}
        return {'backend': 'codex', 'participation': meeting['participation'],
                'model_state': 'running' if self.worker else 'waiting' if meeting['generation']>meeting['decided_generation'] else 'idle',
                'model_error': meeting['error'], 'offered': self.offered, 'newly_persisted': self.persisted,
                'decisions': self.decisions, 'replied': self.replied,
                'material_state':meeting['material_state'] or 'disabled'} | self.data.reply_statistics(self.mid) | live

    def run(self, *, watch=False, notify=None):
        previous = None
        with self.lock:
            try:
                self.data.recover_sends()
                self.data.ensure_session(self.session)
                self.data.ensure_role(self.mid,self.role_version)
                self._ensure_materials()
                if self.settings.introduction:
                    with self._maintenance_lock():
                        if not introduce(self.data,self.manager,clock=self.clock,diagnose=self.diagnose):
                            raise LifecycleError('introduction send not confirmed; check meeting UI and replies, then resolve-reply before model processing')
                while not self.stop_event.is_set():
                    status = self.step()
                    if notify and status != previous:
                        notify(status)
                        previous = status
                    if not watch and self.worker is None:
                        meeting = self.data.meeting(self.mid)
                        if self.model_failure or meeting['generation'] == meeting['decided_generation']:
                            return status
                    self.stop_event.wait(0.1)
            except StopRequested:
                if not self.stop_event.is_set():
                    raise
        return self.statistics()

    def close(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=self.settings.timeout_seconds + 5)
            if self.worker.is_alive():
                raise LifecycleError('model worker still shutting down; retry stopping before closing storage')
        self.backend.close()
        self.data.close()
