"""Bounded worker pool executing persisted tasks from the store.

The store is the source of truth: workers poll ``next_queued`` so restarts and
multiple processes stay consistent. Global concurrency is the worker count.
"""
import json
import threading
import time

from .backends.base import BackendError
from .decisions import InvalidDecision, validate
from .store import GatewayStore


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
        try:
            if self.store.cancel_requested(request_id):
                self.store.finish(request_id, 'cancelled', error_code='cancelled',
                                  timings_json=self._timings(started))
                return
            try:
                backend = self.backend_factory(task['backend'])
                decision = backend.decide(
                    task['system'] or '', json.loads(task['input'] or '{}'),
                    deadline_seconds=task['deadline_seconds'],
                    stop_event=cancel_event,
                    metadata=json.loads(task['metadata'] or '{}'))
            except BackendError as exc:
                self.store.finish(request_id, 'cancelled' if exc.code == 'cancelled' else 'failed',
                                  error_code=exc.code, timings_json=self._timings(started))
                return
            except Exception:
                self.store.finish(request_id, 'failed', error_code='server_error',
                                  timings_json=self._timings(started))
                return
            try:
                normalized = validate(decision)
            except InvalidDecision:
                self.store.finish(request_id, 'failed', error_code='output_invalid',
                                  timings_json=self._timings(started))
                return
            self.store.finish(request_id, 'succeeded',
                              result_json=json.dumps(normalized, ensure_ascii=False),
                              timings_json=self._timings(started))
        finally:
            with self._lock:
                self._cancellations.pop(request_id, None)

    @staticmethod
    def _timings(started: float) -> str:
        return json.dumps({'execution_ms': int((time.monotonic() - started) * 1000)})
