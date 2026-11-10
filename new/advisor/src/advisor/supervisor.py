"""Session supervisor: one consume loop per non-ended session (port of the old
AdviserService per-meeting loop, plan-C adapted).

Loops: lease -> poll -> (expired replies discarded) -> job prepare/execute/
apply -> draining on end command or ingest idle -> auto-ended when drain
conditions hold. Restart recovery: every recording/draining session gets its
loop back on boot.
"""
import logging
import os
import threading
import time

from .errors import StorageError
from .model_jobs import ModelJobExecutor
from .model_backend import load_role
from .settings import AdviserSettings
from .store import SessionStore

LOG = logging.getLogger(__name__)


class SessionSupervisor:
    def __init__(self, store: SessionStore, settings: AdviserSettings, *,
                 backend_factory, ingest_activity=None, close_ingest=None,
                 source_connected=None, clock=time.time, monotonic=time.monotonic):
        self.store = store
        self.settings = settings
        self.backend_factory = backend_factory
        self.ingest_activity = ingest_activity or (lambda: {})
        self.close_ingest = close_ingest or (lambda _sid: None)
        self.source_connected = source_connected
        self.clock = clock
        self.monotonic = monotonic
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._started_at: dict[str, float] = {}
        self._lease_owner = f'advisor-{os.getpid()}'

    def start(self):
        """Restart recovery: resume loops for all non-ended sessions."""
        self.store.recover()
        for session in self.store.db.execute(
                "SELECT session_id,phase FROM sessions WHERE phase IN ('recording','draining')"):
            self.start_session(session['session_id'])

    def start_session(self, session_id):
        with self._guard:
            if session_id in self._threads:
                return
            thread = threading.Thread(target=self._loop, args=(session_id,),
                                      name=f'supervise-{session_id[:10]}', daemon=True)
            self._threads[session_id] = thread
            self._started_at[session_id] = self.monotonic()
        thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        with self._guard:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=timeout)

    def active_sessions(self):
        with self._guard:
            return sorted(self._threads)

    # -- loop --------------------------------------------------------------

    def _loop(self, session_id):
        role, role_version = load_role(self.settings)
        if not self.store.acquire_lease(session_id, self._lease_owner,
                                        ttl_s=30, now=self.clock()):
            LOG.warning('lease busy for %s; loop exits', session_id)
            return
        executor = ModelJobExecutor(self.backend_factory(), role)
        backoff_until = 0.0
        try:
            while not self._stop.is_set():
                if not self.store.acquire_lease(session_id, self._lease_owner,
                                                ttl_s=30, now=self.clock()):
                    LOG.warning('lease lost for %s', session_id)
                    return
                try:
                    session = self.store.require_session(session_id)
                except StorageError:
                    return
                phase = session['phase']
                if phase == 'ended':
                    return
                now = self.clock()
                result = executor.take_finished()
                if result is not None:
                    self._apply_result(result, now)
                    backoff_until = now + self.settings.poll_interval * 5
                if phase == 'draining':
                    self.close_ingest(session_id)
                    self._reap_disconnected_sources(session_id)
                    if not executor.running and self.store.drain_ready(session_id):
                        self.store.set_phase(session_id, 'ended', now=now)
                        LOG.info('session %s drained to ended', session_id)
                        return
                else:
                    if self._idle_expired(session_id):
                        self.store.set_phase(session_id, 'draining', now=now)
                        LOG.info('session %s idle -> draining', session_id)
                    else:
                        self.store.discard_expired_replies(
                            session_id, self.settings, now=now)
                        if not executor.running and now >= backoff_until:
                            self._launch(executor, session_id, role_version, now)
                self._stop.wait(self.settings.poll_interval)
        finally:
            executor.close(timeout=2)
            self.store.release_lease(session_id, self._lease_owner)

    def _launch(self, executor, session_id, role_version, now):
        job = self.store.prepare_job(session_id, self.settings, role_version, now=now)
        if job is None:
            return
        self.store.mark_job_running(job['job_id'])
        executor.start(job)

    def _apply_result(self, result, now):
        if result.error is None and result.decision is not None:
            self.store.finish_job(result.job, result.decision,
                                  now=now, settings=self.settings)
        else:
            self.store.fail_job(result.job, result.error or 'model_failed',
                                retry_at=now + 5, now=now)

    def _reap_disconnected_sources(self, session_id):
        """Live sources with no ingest connection left are terminal (lost)."""
        connected = self.source_connected
        if not callable(connected):
            return
        for source in self.store.live_sources(session_id):
            if not connected(session_id, source):
                self.store.mark_source_lost(session_id, source)

    def _idle_expired(self, session_id):
        activity = self.ingest_activity()
        last = activity.get(session_id)
        started = self._started_at.get(session_id)
        if started is None:
            started = self.monotonic()
            with self._guard:
                self._started_at[session_id] = started
        idle_for = self.monotonic() - (last if last is not None else started)
        return idle_for > self._idle_timeout(session_id)

    def _idle_timeout(self, session_id):
        try:
            session = self.store.require_session(session_id)
            return session['idle_timeout_s']
        except StorageError:
            return self.settings.idle_timeout_s
