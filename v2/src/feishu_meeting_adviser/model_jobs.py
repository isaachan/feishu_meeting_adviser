"""One bounded model worker with no persistence or transport-side effects."""
from dataclasses import dataclass
import json
import queue
import threading
import time

from .llm import ModelBackend, ModelError


@dataclass(frozen=True)
class ModelJobResult:
    job: dict
    decision: object | None
    error: str | None
    elapsed_ms: float


class ModelJobExecutor:
    def __init__(self, backend: ModelBackend, role: str, *, input_observer=None):
        self.backend = backend
        self.role = role
        self.input_observer = input_observer or (lambda _snapshot: None)
        self.worker = None
        self.results = queue.Queue(maxsize=1)

    @property
    def running(self) -> bool:
        return self.worker is not None

    def start(self, job: dict) -> None:
        if self.worker is not None:
            raise RuntimeError('model job executor already has a worker')
        snapshot = json.loads(job['snapshot'])
        self.input_observer(snapshot)

        def work():
            started = time.monotonic()
            try:
                decision = self.backend.decide(snapshot, self.role)
                result = ModelJobResult(job, decision, None, (time.monotonic() - started) * 1000)
            except ModelError as exc:
                result = ModelJobResult(job, None, exc.reason, (time.monotonic() - started) * 1000)
            except Exception:
                result = ModelJobResult(job, None, 'model_failed', (time.monotonic() - started) * 1000)
            self.results.put(result)

        self.worker = threading.Thread(
            target=work, name='meeting-model-decision', daemon=False)
        self.worker.start()

    def take_finished(self) -> ModelJobResult | None:
        if self.worker is None or self.worker.is_alive():
            return None
        self.worker.join()
        self.worker = None
        return self.results.get_nowait()

    def diagnostic_metadata(self) -> dict:
        read = getattr(self.backend, 'diagnostic_metadata', None)
        metadata = read() if callable(read) else {}
        return metadata if isinstance(metadata, dict) else {}

    def close(self, *, timeout: float) -> bool:
        if self.worker:
            self.worker.join(timeout=timeout)
            if self.worker.is_alive():
                return False
            self.worker = None
        self.backend.close()
        return True
