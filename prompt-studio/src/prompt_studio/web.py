"""Web UI state: one replay at a time, driven by a background worker."""
from __future__ import annotations

import threading

from .replay import DEFAULT_PACED_SECONDS, ReplayRun
from .stack import Stack
from .transcript import Transcript

ACTIVE = ('opening', 'running')


class WebError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class Runner:
    def __init__(self, stack: Stack, owner: str):
        self.stack = stack
        self.owner = owner
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._run: ReplayRun | None = None
        self._state = 'idle'
        self._mode = 'auto'
        self._paced_seconds = DEFAULT_PACED_SECONDS
        self._run_id: str | None = None
        self._session_id: str | None = None
        self._result: dict | None = None
        self._error: str | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state in ACTIVE

    @property
    def run_id(self) -> str | None:
        with self._lock:
            return self._run_id

    def start(self, transcript: Transcript, mode: str,
              paced_seconds: float = DEFAULT_PACED_SECONDS) -> str:
        with self._lock:
            if self._state in ACTIVE:
                raise WebError(409, 'run_active', '已有重放正在运行')
            run = ReplayRun(self.stack, transcript, mode=mode, owner=self.owner)
            self._run = run
            self._state = 'opening'
            self._mode = mode
            self._paced_seconds = paced_seconds
            self._run_id = run.run_id
            self._session_id = None
            self._result = None
            self._error = None
            self._thread = threading.Thread(
                target=self._worker, name='prompt-studio-run', daemon=True)
            self._thread.start()
            return run.run_id

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'state': self._state,
                'mode': self._mode,
                'paced_seconds': self._paced_seconds,
                'run_id': self._run_id,
                'session_id': self._session_id,
                'result': self._result,
                'error': self._error,
            }

    def stop(self, timeout: float = 5.0):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _worker(self):
        run = self._run
        assert run is not None
        try:
            with self._lock:
                self._state = 'running'
            if self._mode == 'paced':
                result = run.run_paced(self._paced_seconds)
            else:
                result = run.run_auto()
            with self._lock:
                self._session_id = result.session_id
                self._result = result.to_dict()
                self._state = 'done'
        except BaseException as exc:  # noqa: BLE001 - surface to the UI
            with self._lock:
                self._error = f'{type(exc).__name__}: {exc}'
                self._state = 'error'
        finally:
            try:
                run.close()
            except Exception:
                pass
            with self._lock:
                self._run = None
