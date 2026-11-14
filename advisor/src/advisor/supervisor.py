"""Session supervisor: one consume loop per non-ended session (port of the old
AdviserService per-meeting loop, plan-C adapted).

Loops: lease -> poll -> (expired replies discarded) -> job prepare/execute/
apply -> draining on end command or ingest idle -> auto-ended when drain
conditions hold. Restart recovery: every recording/draining session gets its
loop back on boot.
"""
import logging
import json
import os
import threading
import time

from .auth_client import AuthorizationRequired
from .errors import StorageError
from .model_jobs import ModelJobExecutor
from .model_backend import load_role
from .nsdp import NsdpUnavailable
from .settings import AdviserSettings
from .store import SessionStore

LOG = logging.getLogger(__name__)


class SessionSupervisor:
    def __init__(self, store: SessionStore, settings: AdviserSettings, *,
                 backend_factory, ingest_activity=None, close_ingest=None,
                 source_connected=None, clock=time.time, monotonic=time.monotonic,
                 nsdp=None, legacy_nsdp=False):
        self.store = store
        self.settings = settings
        self.backend_factory = backend_factory
        self.ingest_activity = ingest_activity or (lambda: {})
        self.close_ingest = close_ingest or (lambda _sid: None)
        self.source_connected = source_connected
        self.clock = clock
        self.monotonic = monotonic
        self.nsdp = nsdp
        self.legacy_nsdp = legacy_nsdp
        self._nsdp_next: dict[str, float] = {}
        self._nsdp_last_nag: dict[str, float] = {}
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._started_at: dict[str, float] = {}
        self._lease_owner = f'advisor-{os.getpid()}'

    def start(self):
        """Restart recovery: resume loops for all non-ended sessions."""
        self.store.recover()
        self.store.clear_leases()  # single-host v1: prior holders are gone
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
        backend = self.backend_factory()
        if hasattr(backend, 'owner_open_id'):
            backend.owner_open_id = self.store.require_session(session_id)['owner_open_id']
        if hasattr(backend, 'session_id'):
            backend.session_id = session_id
        prepare = getattr(backend, 'prepare_session', None)
        if callable(prepare):
            prepare()
        executor = ModelJobExecutor(backend, role)
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
                if not self.legacy_nsdp and session['nsdp_hold']:
                    self.store.set_nsdp_hold(session_id, False, now=self.clock())
                    session = self.store.require_session(session_id)
                phase = session['phase']
                if phase == 'ended':
                    return
                now = self.clock()
                if self.legacy_nsdp:
                    self._nsdp_maintenance(session_id, session, now)
                result = executor.take_finished()
                if result is not None:
                    self._apply_result(result, now, executor.diagnostic_metadata())
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
                        expired = self.store.discard_expired_replies(
                            session_id, self.settings, now=now)
                        if expired:
                            LOG.info('session %s expired replies cancelled: %s',
                                     session_id, expired)
                        if not executor.running and now >= backoff_until:
                            self._launch(executor, session_id, role_version, now)
                self._stop.wait(self.settings.poll_interval)
        finally:
            executor.close(timeout=2)
            self.store.release_lease(session_id, self._lease_owner)

    def _launch(self, executor, session_id, role_version, now):
        session = self.store.session(session_id)
        if self.legacy_nsdp and session is not None and session['nsdp_hold']:
            return  # hard gate: no model decisions while NSDP authorization is missing
        job = self.store.prepare_job(session_id, self.settings, role_version, now=now)
        if job is None:
            return
        snapshot = json.loads(job['snapshot'])
        LOG.info('model job start: session=%s job=%s generation=%s captions=%s '
                 'fresh_keys=%s pending_reply=%s', session_id, job['job_id'][:12],
                 job['generation'], len(snapshot.get('captions', [])),
                 snapshot.get('fresh_input_keys'),
                 bool(snapshot.get('pending_reply')))
        self.store.mark_job_running(job['job_id'])
        executor.start(job)

    def _apply_result(self, result, now, metadata=None):
        timings = (metadata or {}).get('remote_timings') or {}
        if (self.nsdp is not None and
                'nsdp_authorization_required' in timings.get('tool_events', [])):
            session = self.store.session(result.job['session_id'])
            if session:
                self.nsdp.notify_owner(result.job['session_id'], session['owner_open_id'],
                                       dedupe_s=self.settings.nsdp_nag_seconds)
        if result.error is None and result.decision is not None:
            self.store.finish_job(result.job, result.decision,
                                  now=now, settings=self.settings)
            LOG.info('model decision applied: session=%s job=%s control=%s '
                     'reply="%s" reply_keys=%s elapsed_ms=%.0f',
                     result.job['session_id'], result.job['job_id'][:12],
                     result.decision.control,
                     (result.decision.reply or '')[:120],
                     list(result.decision.reply_sentence_keys),
                     result.elapsed_ms)
        else:
            self.store.fail_job(result.job, result.error or 'model_failed',
                                retry_at=now + 5, now=now)
            LOG.warning('model job failed: session=%s job=%s reason=%s '
                        'elapsed_ms=%.0f', result.job['session_id'],
                        result.job['job_id'][:12], result.error, result.elapsed_ms)

    def _reap_disconnected_sources(self, session_id):
        """Live sources with no ingest connection left are terminal (lost)."""
        connected = self.source_connected
        if not callable(connected):
            return
        for source in self.store.live_sources(session_id):
            if not connected(session_id, source):
                self.store.mark_source_lost(session_id, source)

    # -- NSDP maintenance (hold / nag / resume) --------------------------------

    def _nsdp_maintenance(self, session_id, session, now):
        """Periodic refresh while healthy; fast retry + nag DMs while held."""
        if self.nsdp is None or now < self._nsdp_next.get(session_id, 0.0):
            return
        if session['nsdp_hold']:
            self._nsdp_next[session_id] = now + self.settings.nsdp_retry_seconds
            self._nsdp_try_resume(session_id, session, now)
        else:
            self._nsdp_next[session_id] = now + self.settings.nsdp_refresh_seconds
            self._nsdp_refresh(session_id, session, now)

    def _nsdp_clues(self, session_id):
        material = self.store.materials(session_id) or {}
        return material.get('description') or ''

    def _nsdp_refresh(self, session_id, session, now):
        try:
            self.nsdp.prefetch(session['owner_open_id'], session_id,
                               self._nsdp_clues(session_id))
        except AuthorizationRequired:
            self.store.set_nsdp_hold(session_id, True, now=now)
            self._nsdp_nag(session_id, session['owner_open_id'], now)
            # a fresh 401 switches immediately to fast-retry cadence
            self._nsdp_next[session_id] = now + self.settings.nsdp_retry_seconds
            LOG.warning('session %s nsdp hold: token missing/rejected', session_id)
        except NsdpUnavailable as exc:
            LOG.warning('session %s nsdp refresh unavailable: %s', session_id, exc)

    def _nsdp_try_resume(self, session_id, session, now):
        try:
            self.nsdp.prefetch(session['owner_open_id'], session_id,
                               self._nsdp_clues(session_id))
        except AuthorizationRequired:
            self._nsdp_nag(session_id, session['owner_open_id'], now)
        except NsdpUnavailable as exc:
            LOG.warning('session %s nsdp resume unavailable: %s', session_id, exc)
        else:
            self.store.set_nsdp_hold(session_id, False, now=now)
            self._nsdp_next[session_id] = now + self.settings.nsdp_refresh_seconds
            LOG.info('session %s nsdp authorization restored; decisions resumed',
                     session_id)

    def _nsdp_nag(self, session_id, owner_open_id, now):
        if now - self._nsdp_last_nag.get(session_id, 0.0) < self.settings.nsdp_nag_seconds:
            return
        self._nsdp_last_nag[session_id] = now
        self.nsdp.notify_owner(session_id, owner_open_id,
                               dedupe_s=self.settings.nsdp_nag_seconds)

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
