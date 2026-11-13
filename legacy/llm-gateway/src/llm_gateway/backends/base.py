"""Backend contract for gateway-side model execution."""
import threading


class BackendError(Exception):
    """Execution failure with a contract error code; never carries payload text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ModelBackend:
    """One blocking decision per call; implementations own their process lifetime."""

    def decide(self, system: str, input_value: dict, *, deadline_seconds: int,
               stop_event: threading.Event, metadata: dict) -> dict:
        raise NotImplementedError
