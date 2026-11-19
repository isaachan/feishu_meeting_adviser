"""Bounded worker pool executing persisted tasks from the store.

The store is the source of truth: workers poll ``next_queued`` so restarts and
multiple processes stay consistent. Global concurrency is the worker count.
"""
import json
import logging
import threading
import time

from .backends.base import BackendError
from .decisions import InvalidDecision, validate
from .store import GatewayStore

LOG = logging.getLogger(__name__)


class ExecutionScheduler:
    def __init__(self, store: GatewayStore, backend_factory, *, workers: int = 4):
        if workers < 1:
            raise ValueError('workers must be positive')
        self.store = store
        self.backend_factory = backend_factory
        self.stop_event = threading.Event()
        self._cancellations: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._worker, name=f'gateway-worker-{n}',
                                          daemon=True) for n in range(workers)]

    def start(self):
        for thread in self._threads:
            thread.start()

    def stop(self, *, timeout: float = 5.0) -> bool:
        self.stop_event.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        return not any(thread.is_alive() for thread in self._threads)

    def notify_cancel(self, request_id: str):
        with self._lock:
            event = self._cancellations.get(request_id)
        if event is not None:
            event.set()

    def _worker(self):
        while not self.stop_event.is_set():
            task = self.store.next_queued()
            if task is None:
                self.stop_event.wait(0.05)
                continue
            self._execute(task)

    def _execute(self, task):
        request_id = task['request_id']
        cancel_event = threading.Event()
        with self._lock:
            self._cancellations[request_id] = cancel_event
        started = time.monotonic()
        metadata = json.loads(task['metadata'] or '{}')
        session_id = metadata.get('session_id')
        LOG.info('execution start: request_id=%s backend=%s', request_id,
                 task['backend'])
        try:
            if self.store.cancel_requested(request_id):
                self.store.finish(request_id, 'cancelled', error_code='cancelled',
                                  timings_json=self._timings(started))
                LOG.info('execution cancelled (pre-start): request_id=%s', request_id)
                return
            backend = None
            try:
                backend = self.backend_factory(task['backend'])
                backend.timing_request_id = request_id
                decision = backend.decide(
                    task['system'] or '', json.loads(task['input'] or '{}'),
                    deadline_seconds=task['deadline_seconds'],
                    stop_event=cancel_event,
                    metadata=metadata)
            except BackendError as exc:
                self.store.finish(request_id, 'cancelled' if exc.code == 'cancelled' else 'failed',
                                  error_code=exc.code,
                                  timings_json=self._timings(started, backend))
                self._log_execution(request_id, session_id,
                                    'cancelled' if exc.code == 'cancelled' else 'failed',
                                    exc.code, started, backend)
                LOG.warning('execution backend error: request_id=%s code=%s '
                            'execution_ms=%s', request_id, exc.code,
                            int((time.monotonic() - started) * 1000))
                return
            except Exception:
                self.store.finish(request_id, 'failed', error_code='server_error',
                                  timings_json=self._timings(started, backend))
                self._log_execution(request_id, session_id, 'failed', 'server_error',
                                    started, backend)
                LOG.exception('execution crashed: request_id=%s', request_id)
                return
            try:
                normalized = validate(decision)
            except InvalidDecision:
                self.store.finish(request_id, 'failed', error_code='output_invalid',
                                  timings_json=self._timings(started, backend))
                self._log_execution(request_id, session_id, 'failed', 'output_invalid',
                                    started, backend)
                LOG.warning('execution output invalid: request_id=%s', request_id)
                return
            self.store.finish(request_id, 'succeeded',
                              result_json=json.dumps(normalized, ensure_ascii=False),
                              timings_json=self._timings(started, backend))
            self._log_execution(request_id, session_id, 'succeeded', None,
                                started, backend)
            LOG.info('execution done: request_id=%s execution_ms=%s reply="%s"',
                     request_id, int((time.monotonic() - started) * 1000),
                     str(normalized.get('reply') or '')[:120])
        finally:
            with self._lock:
                self._cancellations.pop(request_id, None)

    @staticmethod
    def _timings(started: float, backend=None) -> str:
        return json.dumps(ExecutionScheduler._timings_dict(started, backend))

    @staticmethod
    def _timings_dict(started: float, backend=None) -> dict:
        result: dict = {'execution_ms': int((time.monotonic() - started) * 1000)}
        if backend is not None:
            stages = getattr(backend, 'stage_timings', None)
            if isinstance(stages, dict) and stages:
                result.update({key: value for key, value in stages.items()
                               if key.endswith('_ms') and isinstance(value, int)})
            failed_stage = getattr(backend, 'failed_stage', None)
            if failed_stage is not None:
                result['failed_stage'] = failed_stage
            attempts = getattr(backend, 'attempts', 0)
            if isinstance(attempts, int) and attempts > 1:
                result['attempts'] = attempts
            tools = getattr(backend, 'tool_timings', None)
            if isinstance(tools, list) and tools:
                result['tool_calls'] = tools[:128]
            stats = getattr(backend, 'resource_stats', None)
            if isinstance(stats, dict) and stats:
                result.update(stats)
            events = getattr(backend, 'tool_events', set())
            allowed = sorted(set(events) & {'nsdp_authorization_required',
                                            'jira_authorization_required'})
            if allowed:
                result['tool_events'] = allowed
        return result

    def _log_execution(self, request_id, session_id, state, error_code,
                       started: float, backend):
        timings = self._timings_dict(started, backend)
        event = {'event': 'task_timings', 'request_id': request_id, 'state': state,
                 'timings': timings}
        if session_id is not None:
            event['session_id'] = session_id
        if error_code is not None:
            event['error_code'] = error_code
        LOG.info('timing_event: %s', json.dumps(event, ensure_ascii=False))
