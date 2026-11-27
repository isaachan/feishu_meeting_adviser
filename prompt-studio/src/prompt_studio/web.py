"""Web UI state: one replay at a time, driven by a background worker."""
from __future__ import annotations

import queue
import threading

from .replay import ReplayRun
from .results import ReplayResult
from .stack import Stack
from .transcript import Transcript

ACTIVE = ('opening', 'running', 'step')


class WebError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def step_view(index: int, turn, result: ReplayResult) -> dict:
    jobs = result.complete_jobs or result.jobs
    job = jobs[-1] if jobs else None
    return {
        'index': index,
        'speaker': turn.speaker,
        'text': turn.text,
        'job_state': job.state if job else None,
        'decision': job.decision if job else None,
        'reply': (job.decision or {}).get('reply') if job and job.decision else None,
        'records': result.records,
        'jobs': [item.to_dict() for item in result.jobs],
    }


class Runner:
    def __init__(self, stack: Stack, owner: str):
        self.stack = stack
        self.owner = owner
        self._lock = threading.Lock()
        self._commands: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._run: ReplayRun | None = None
        self._state = 'idle'
        self._mode = 'auto'
        self._run_id: str | None = None
        self._session_id: str | None = None
        self._steps: list[dict] = []
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

    def start(self, transcript: Transcript, mode: str) -> str:
        with self._lock:
            if self._state in ACTIVE:
                raise WebError(409, 'run_active', '已有重放正在运行')
            while True:
                try:
                    self._commands.get_nowait()
                except queue.Empty:
                    break
            run = ReplayRun(self.stack, transcript, mode=mode, owner=self.owner)
            self._run = run
            self._state = 'opening'
            self._mode = mode
            self._run_id = run.run_id
            self._session_id = None
            self._steps = []
            self._result = None
            self._error = None
            self._thread = threading.Thread(
                target=self._worker, name='prompt-studio-run', daemon=True)
            self._thread.start()
            return run.run_id

    def next_step(self) -> dict:
        with self._lock:
            if self._state != 'step':
                raise WebError(409, 'not_step', '当前不在 step 等待状态')
            self._commands.put('next')
        return self.snapshot()

    def finish(self) -> dict:
        with self._lock:
            if self._state not in ACTIVE:
                raise WebError(409, 'not_active', '没有进行中的重放')
            self._commands.put('finish')
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'state': self._state,
                'mode': self._mode,
                'run_id': self._run_id,
                'session_id': self._session_id,
                'steps': [dict(step) for step in self._steps],
                'result': self._result,
                'error': self._error,
            }

    def stop(self, timeout: float = 5.0):
        with self._lock:
            active = self._state in ACTIVE
            if active:
                self._commands.put('finish')
            thread = self._thread
        if active and thread is not None:
            thread.join(timeout=timeout)

    def _worker(self):
        run = self._run
        assert run is not None
        try:
            if self._mode == 'auto':
                with self._lock:
                    self._state = 'running'
                result = run.run_auto()
                with self._lock:
                    self._session_id = result.session_id
                    self._result = result.to_dict()
                    self._state = 'done'
                return
            run.open()
            with self._lock:
                self._session_id = run.session_id
                self._state = 'step'
            while True:
                command = self._commands.get()
                if command in ('finish', 'cancel'):
                    break
                if command != 'next':
                    continue
                with self._lock:
                    self._state = 'running'
                turn = run.send_next()
                if turn is None:
                    break
                result = run.collect()
                with self._lock:
                    self._steps.append(
                        step_view(len(self._steps) + 1, turn, result))
                    self._state = 'step'
            run.finish()
            result = run.collect()
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
