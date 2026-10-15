"""Application service orchestrating durable input, model work and reply dispatch."""
from contextlib import contextmanager
import threading
import time

from .client import StopRequested
from .config import file_fingerprint
from .diagnostics import sanitize
from .infrastructure.errors import LifecycleError, LockBusy
from .infrastructure.file_lock import FileLock
from .llm import create_backend, ModelError, load_role
from .maintenance import Maintenance
from .reply_sender import introduce
from .models import digest
from .materials import MATERIAL_POLICY, load_materials
from .adapters.persistence.factory import StorageFactory
from .model_jobs import ModelJobExecutor
from .reply_dispatcher import ReplyDispatcher
from .material_bootstrap import MaterialBootstrapper


class AdviserService:
    def __init__(self, source, manager, *, backend=None, stop_event=None, clock=time.time, consumer_id='adviser', limit=100, diagnose=None,
                 material_client=None, preview_display=None, material_loader=load_materials,
                 model_input_observer=None, decision_observer=None):
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
        self.data = StorageFactory(self.config).adviser()
        self.data.ensure_meeting(self.mid)
        self.maintenance = Maintenance(source, self.config)
        self.lock = FileLock(self.config.data_directory / 'locks' / 'adviser.lock')
        runtime = self.config.data_directory / 'model_runtime'
        if self.settings.backend == 'opencode':
            runtime = runtime / 'opencode' / digest(self.mid)
        self.backend = backend or create_backend(self.settings, runtime, stop_event=self.stop_event)
        self.executor = ModelJobExecutor(
            self.backend, self.role, input_observer=model_input_observer)
        self.dispatcher = ReplyDispatcher(
            self.data,
            self.source,
            self.manager.client,
            meeting_id=self.mid,
            consumer_id=self.consumer_id,
            settings=self.settings,
            session=self.session,
            clock=lambda: self.clock(),
            diagnose=self.diagnose,
        )
        self.batch_due = None
        self.offered = self.persisted = self.decisions = self.replied = 0
        self.model_failure = None
        self.materials = MaterialBootstrapper(
            self.data,
            self.config,
            self.session,
            client=material_client,
            loader=material_loader,
            preview_display=preview_display,
            stop_event=self.stop_event,
            clock=lambda: self.clock(),
            maintenance_lock=self._maintenance_lock,
            guard=self.guard,
        )
        self.decision_observer = decision_observer or (lambda decision:None)

    @property
    def worker(self):
        return self.executor.worker

    @property
    def results(self):
        return self.executor.results

    def _ensure_materials(self):
        self.materials.ensure()

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
        if self.source.is_deleted(self.mid):
            raise LifecycleError('meeting content was deleted; model consumption cannot restart for this meeting')
        if self.manager.session() != self.session:
            raise LifecycleError('adviser session changed; restart after checking meeting state')
        if file_fingerprint(self.config.path) != self.config.fingerprint:
            raise LifecycleError('adviser configuration changed; restart consumer')
        if load_role(self.settings)[1] != self.role_file_version:
            raise LifecycleError('adviser role changed; restart consumer')
        self.materials.verify_identity()
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
        self.executor.start(job)

    def _finish_job(self, job, decision, elapsed):
        self.decision_observer(decision)
        self.data.finish_job(job, decision, now=self.clock(), settings=self.settings,
                             joined_at=self.session['joined_at'], bot_id=self.session.get('bot_user_id'))
        self.decisions += 1
        self.diagnose(self._model_metadata() | {'kind':'model','backend':self.settings.backend,'model_ms':elapsed,'decision_saved':True,
                       'participation':self.data.require_meeting(self.mid)['participation']})

    def _model_metadata(self):
        metadata = self.executor.diagnostic_metadata()
        return sanitize(metadata | {'kind': 'model'}) if isinstance(metadata, dict) else {}

    def dispatch(self):
        if self.worker is not None or self.stop_event.is_set():
            return
        with self._maintenance_lock():
            self.guard()
            # Source may have committed captions after this step's ingest or model completion.
            self.replied += int(self.dispatcher.dispatch())

    def step(self):
        self._ensure_materials()
        self.ingest()
        with self._maintenance_lock():
            self.dispatcher.discard_expired()
        completed = self.executor.take_finished()
        if completed is not None:
            with self._maintenance_lock():
                self.guard()
                job, decision, error, elapsed = (
                    completed.job, completed.decision, completed.error, completed.elapsed_ms)
                if error:
                    self.data.fail_job(job, error, retry_at=self.clock()+self.config.cooldown_seconds)
                    self.model_failure = error
                    self.diagnose(self._model_metadata() | {'kind':'model','backend':self.settings.backend,'error_kind':error,'model_ms':elapsed})
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
                    role_characters=len(self.role)+getattr(self.backend,'context_overhead_characters',0))
            if job:
                self._start_job(job)
            self.batch_due = None
        self.dispatch()
        return self.statistics()

    def statistics(self):
        meeting = self.data.require_meeting(self.mid)
        # Avoid changing elapsed counters on each watch tick and flooding stdout.
        stage = self._model_metadata().get('model_stage')
        live = {'model_stage': stage} if isinstance(stage, str) and stage in {
            'startup','initialize','account_read','mcp_discovery','thread_start',
            'mcp_verify','turn_start','generation','thread_cleanup','server_start','server_verify',
            'session_start','session_cleanup'} else {}
        return {'backend': self.settings.backend, 'participation': meeting['participation'],
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
                        meeting = self.data.require_meeting(self.mid)
                        if self.model_failure or meeting['generation'] == meeting['decided_generation']:
                            return status
                    self.stop_event.wait(0.1)
            except StopRequested:
                if not self.stop_event.is_set():
                    raise
        return self.statistics()

    def close(self):
        self.stop_event.set()
        if not self.executor.close(timeout=self.settings.timeout_seconds + 5):
            raise LifecycleError('model worker still shutting down; retry stopping before closing storage')
        self.data.close()


# Compatibility for callers that imported the pre-refactor orchestration name.
AdviserRunner = AdviserService
