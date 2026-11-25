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

from .errors import ModelError, StorageError
from .model_jobs import ModelJobExecutor
from .model_backend import load_role
from .settings import AdviserSettings
from .store import SessionStore

LOG = logging.getLogger(__name__)


class SessionSupervisor:
    def __init__(self, store: SessionStore, settings: AdviserSettings, *,
                 nsdp, backend_factory, ingest_activity=None, close_ingest=None,
                 source_connected=None, clock=time.time, monotonic=time.monotonic,
                 jira_notifier=None, meeting_observer=None):
        self.store = store
        self.settings = settings
        self.backend_factory = backend_factory
        self.ingest_activity = ingest_activity or (lambda: {})
        self._close_ingest = close_ingest
        self.meeting_observer = meeting_observer
        self.source_connected = source_connected
        self.clock = clock
        self.monotonic = monotonic
        self.nsdp = nsdp
        self.jira_notifier = jira_notifier or nsdp
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._started_at: dict[str, float] = {}
        self._lease_owner = f'advisor-{os.getpid()}'

    def close_ingest(self, session_id, *, paused=False):
        if self._close_ingest is None:
            return
        if paused:
            self._close_ingest(session_id, error_code='session_paused')
        else:
            self._close_ingest(session_id)

    def start(self):
        """Restart recovery: resume loops for all non-ended sessions."""
        self.store.recover()
        self.store.clear_leases()  # single-host v1: prior holders are gone
        for session in self.store.db.execute(
                "SELECT session_id,phase FROM sessions WHERE phase IN ('running','paused','draining')"):
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
        next_observation_at = 0.0
        try:
            while not self._stop.is_set():
                if not self.store.acquire_lease(session_id, self._lease_owner,
                                                ttl_s=30, now=self.clock()):
                    LOG.warning('lease lost for %s', session_id)
                    return
                try:
                    try:
                        session = self.store.require_session(session_id)
                    except StorageError:
                        return
                    phase = session['phase']
                    if phase == 'ended':
                        return
                    now = self.clock()
                    self.store.expire_subscription_requests(session_id, now=now)
                    if (self.meeting_observer is not None
                            and phase in ('running', 'paused')
                            and now >= next_observation_at):
                        next_observation_at = now + self.settings.observe_poll_s
                        try:
                            self.meeting_observer.observe(session)
                        except Exception:
                            LOG.exception('meeting observation failed: session=%s', session_id)
                        session = self.store.require_session(session_id)
                        phase = session['phase']
                        now = self.clock()
                    if phase == 'paused' and executor.running:
                        stop_event = getattr(backend, 'stop_event', None)
                        if isinstance(stop_event, threading.Event):
                            stop_event.set()  # gateway POST /cancel; local job already abandoned
                    result = executor.take_finished()
                    if result is not None:
                        self._apply_result(result, now, executor.diagnostic_metadata())
                        stop_event = getattr(backend, 'stop_event', None)
                        if isinstance(stop_event, threading.Event):
                            stop_event.clear()  # reuse backend after owner returns
                        backoff_until = now + self.settings.poll_interval * 5
                    if phase == 'draining':
                        self.close_ingest(session_id)
                        self._reap_disconnected_sources(session_id)
                        stuck = self.store.expire_stuck_sending(session_id, now=now)
                        if stuck:
                            LOG.info('session %s stuck sending -> uncertain: %s',
                                     session_id, stuck)
                        expired = self.store.discard_expired_replies(
                            session_id, self.settings, now=now)
                        if expired:
                            LOG.info('session %s expired replies cancelled: %s',
                                     session_id, expired)
                        deadline = session['drain_deadline_at']
                        if deadline is None:
                            deadline = self.store.ensure_drain_deadline(
                                session_id, now=now,
                                duration_s=self.settings.drain_deadline_s)
                        if now >= deadline:
                            # S6: bounded finish even with bot/gateway/STT down
                            diagnostic = self.store.force_end(session_id, now=now)
                            LOG.warning('session %s drain deadline forced ended: %s',
                                        session_id, diagnostic)
                            return
                        if not executor.running and self.store.drain_ready(session_id):
                            self.store.set_phase(session_id, 'ended', now=now)
                            LOG.info('session %s drained to ended', session_id)
                            return
                    else:
                        if phase == 'paused':
                            # owner is away: refuse live capture (idempotent sweep)
                            self.close_ingest(session_id, paused=True)
                        if self._idle_expired(session_id, session):
                            self.store.set_phase(
                                session_id, 'draining', now=now,
                                drain_deadline_at=now + self.settings.drain_deadline_s)
                            LOG.info('session %s idle -> draining (phase=%s)', session_id, phase)
                        elif phase == 'paused':
                            pass  # 不发起新任务；恢复由观测输入驱动（STATE_MACHINES §4）
                        else:
                            expired = self.store.discard_expired_replies(
                                session_id, self.settings, now=now)
                            if expired:
                                LOG.info('session %s expired replies cancelled: %s',
                                         session_id, expired)
                            if not executor.running and now >= backoff_until:
                                self._launch(executor, session_id, role_version, now)
                    self._stop.wait(self.settings.poll_interval)
                except (ModelError, StorageError) as exc:
                    # one bad result must never take the whole session's supervisor down
                    LOG.warning('supervisor iteration failed: session=%s error=%s',
                                session_id, exc)
                    self._stop.wait(self.settings.poll_interval)
                except Exception:
                    LOG.exception('supervisor iteration crashed: session=%s', session_id)
                    self._stop.wait(self.settings.poll_interval)
        finally:
            executor.close(timeout=2)
            self.store.release_lease(session_id, self._lease_owner)

    def _launch(self, executor, session_id, role_version, now):
        job = self.store.prepare_job(session_id, self.settings, role_version, now=now)
        if job is None:
            return
        if job['state'] != 'pending':
            # orphaned inflight row (apply crashed mid-iteration): consume it so
            # the loop does not wedge re-claiming a job that can never start
            LOG.warning('model job not pending; rejected: session=%s job=%s state=%s',
                        session_id, job['job_id'][:12], job['state'])
            try:
                self.store.reject_job(job, 'model_failed', now=now)
            except StorageError:
                pass
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
        session = self.store.session(result.job['session_id'])
        state = self.store.db.execute(
            'SELECT state FROM jobs WHERE job_id=?',
            (result.job['job_id'],)).fetchone()
        if (session is None or session['phase'] in ('paused', 'ended')
                or (state is not None and state['state'] == 'abandoned')):
            LOG.info('late gateway result ignored: session=%s', result.job['session_id'])
            return
        timings = (metadata or {}).get('remote_timings') or {}
        if timings:
            persisted = dict(timings)
            request_id = (metadata or {}).get('remote_request_id')
            if isinstance(request_id, str):
                persisted['request_id'] = request_id
            self.store.record_job_timings(result.job['job_id'], persisted)
        if 'nsdp_authorization_required' in timings.get('tool_events', []):
            session = self.store.session(result.job['session_id'])
            if session:
                self.nsdp.notify_owner(result.job['session_id'], session['owner_open_id'],
                                       dedupe_s=self.settings.nsdp_nag_seconds)
        if 'jira_authorization_required' in timings.get('tool_events', []):
            session = self.store.session(result.job['session_id'])
            if session:
                self.jira_notifier.notify_jira_owner(result.job['session_id'], session['owner_open_id'],
                                                     dedupe_s=self.settings.nsdp_nag_seconds)
        if result.error is None and result.decision is not None:
            try:
                self.store.finish_job(result.job, result.decision,
                                      now=now, settings=self.settings)
            except (ModelError, StorageError) as exc:
                self._reject_result(result, exc, now=now)
                return
            LOG.info('model decision applied: session=%s job=%s control=%s '
                     'reply="%s" reply_keys=%s elapsed_ms=%.0f',
                     result.job['session_id'], result.job['job_id'][:12],
                     result.decision.control,
                     (result.decision.reply or '')[:120],
                     list(result.decision.reply_sentence_keys),
                     result.elapsed_ms)
        else:
            try:
                self.store.fail_job(result.job, result.error or 'model_failed',
                                    retry_at=now + 5)
            except StorageError:
                # job was abandoned (paused/drain discard): late failures drop
                LOG.info('late job failure dropped (job no longer running): '
                         'session=%s job=%s', result.job['session_id'],
                         result.job['job_id'][:12])
                return
            LOG.warning('model job failed: session=%s job=%s reason=%s '
                        'elapsed_ms=%.0f', result.job['session_id'],
                        result.job['job_id'][:12], result.error, result.elapsed_ms)

    def _reject_result(self, result, exc, *, now):
        """Terminal-reject a result the adviser refuses to apply (e.g. evidence
        keys that are not part of the snapshot).

        The job is abandoned instead of retried — otherwise the same snapshot
        would be re-judged forever — and the generation it covered is consumed
        so later captions can still produce a decision.
        """
        reason = getattr(exc, 'reason', 'model_failed')
        LOG.warning('model result rejected: session=%s job=%s reason=%s error=%s',
                    result.job['session_id'], result.job['job_id'][:12],
                    reason, exc)
        try:
            self.store.reject_job(result.job, reason, now=now)
        except StorageError:
            LOG.info('late rejected result dropped (job no longer inflight): '
                     'session=%s job=%s', result.job['session_id'],
                     result.job['job_id'][:12])

    def _reap_disconnected_sources(self, session_id):
        """Live sources with no ingest connection left are terminal (lost)."""
        connected = self.source_connected
        if not callable(connected):
            return
        for source in self.store.live_sources(session_id):
            if not connected(session_id, source):
                self.store.mark_source_lost(session_id, source)

    def _idle_expired(self, session_id, session=None):
        """STATE_MACHINES v2 §1.1: absolute persisted deadline, real audio only.

        deadline = max(started_at, last_audio_at) + idle_timeout_s — wall
        clock, restart-safe; heartbeats/control frames never touch
        last_audio_at (only ingest audio acks do). Paused does not freeze
        the clock: the same deadline applies.
        """
        try:
            row = session or self.store.require_session(session_id)
        except StorageError:
            return False
        timeout = row['idle_timeout_s'] if row['idle_timeout_s'] is not None \
            else self.settings.idle_timeout_s
        base = row['last_audio_at'] or row['started_at']
        return self.clock() >= base + timeout

    def _idle_timeout(self, session_id):
        try:
            session = self.store.require_session(session_id)
            return session['idle_timeout_s']
        except StorageError:
            return self.settings.idle_timeout_s
