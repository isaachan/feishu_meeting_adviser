"""Independent durable inbox and one bounded model worker per meeting consumer."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import threading
import time

from .adviser_store import AdviserStore
from .config import file_fingerprint
from .lifecycle import FileLock, LifecycleError
from .llm import CodexBackend, ModelError, load_role
from .maintenance import Maintenance


class AdviserRunner:
    def __init__(self, source, manager, *, backend=None, stop_event=None, clock=time.time, consumer_id='adviser', limit=100):
        self.source, self.manager = source, manager
        self.config, self.settings = manager.config, manager.config.llm
        self.session = manager.saved_meeting(require_joined=True)
        self.mid = self.session['meeting_id']
        self.clock = clock
        self.consumer_id, self.limit = consumer_id, limit
        self.stop_event = stop_event or threading.Event()
        self.role, self.role_version = load_role(self.settings)
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

    def guard(self):
        if self.manager.session() != self.session:
            raise LifecycleError('adviser session changed; restart after checking meeting state')
        if file_fingerprint(self.config.path) != self.config.fingerprint:
            raise LifecycleError('adviser configuration changed; restart consumer')
        if load_role(self.settings)[1] != self.role_version:
            raise LifecycleError('adviser role changed; restart consumer')

    def ingest(self):
        with self.maintenance.lock():
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
        self.data.finish_job(job, decision)
        self.decisions += 1

    def step(self):
        self.ingest()
        if self.worker is not None and not self.worker.is_alive():
            self.worker.join()
            self.worker = None
            job, decision, error, elapsed = self.results.get_nowait()
            with self.maintenance.lock():
                self.guard()
                if error:
                    self.data.fail_job(job, error, retry_at=self.clock()+self.config.cooldown_seconds)
                    self.model_failure = error
                else:
                    self._finish_job(job, decision, elapsed)
                    self.model_failure = None
        if self.worker is None and (self.batch_due is None or self.clock() >= self.batch_due):
            with self.maintenance.lock():
                self.guard()
                job = self.data.prepare_job(self.mid, self.settings, self.role_version,
                    now=self.clock(), joined_at=self.session['joined_at'], bot_id=self.session.get('bot_user_id'))
            if job:
                self._start_job(job)
            self.batch_due = None
        return self.statistics()

    def statistics(self):
        meeting = self.data.meeting(self.mid)
        return {'backend': 'codex', 'participation': meeting['participation'],
                'model_state': 'running' if self.worker else 'waiting' if meeting['generation']>meeting['decided_generation'] else 'idle',
                'model_error': meeting['error'], 'offered': self.offered, 'newly_persisted': self.persisted,
                'decisions': self.decisions, 'replied': self.replied}

    def run(self, *, watch=False, notify=None):
        previous = None
        with self.lock:
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
        return self.statistics()

    def close(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=self.settings.timeout_seconds + 5)
            if self.worker.is_alive():
                raise LifecycleError('model worker still shutting down; retry stopping before closing storage')
        self.backend.close()
        self.data.close()
