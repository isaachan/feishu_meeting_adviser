"""RemoteModelBackend: decide() over the localhost inference contract.

Implements docs/CONTRACT_REMOTE_INFERENCE.md sections 8.1-8.3: the request id
and content hash are derived deterministically from the payload, so a retry
after a network failure or process restart reattaches the same remote task
instead of starting a second execution.
"""
import json
import threading
import time
from urllib import error, request

from .llm import Decision, LLMSettings, ModelError

ERROR_MAP = {
    'invalid_request': 'model_protocol',
    'payload_too_large': 'model_context_budget',
    'content_conflict': 'model_protocol',
    'not_found': 'model_unavailable',
    'quota_exceeded': 'model_unavailable',
    'backend_unavailable': 'model_unavailable',
    'execution_timeout': 'model_timeout',
    'output_invalid': 'model_invalid_decision',
    'server_error': 'model_unavailable',
}


def remote_identity(system: str, input_value: dict, settings: LLMSettings) -> tuple[str, str]:
    canonical = json.dumps({'system': system, 'input': input_value,
                            'output': 'decision-v1', 'backend': settings.remote_backend,
                            'model': None},
                           sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    import hashlib
    content_hash = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return 'rm' + content_hash, content_hash


class RemoteModelBackend:
    """Blocking decision call with idempotent submit and poll-based recovery."""

    context_overhead_characters = 0

    def __init__(self, settings: LLMSettings, workdir=None, *, stop_event=None, opener=None):
        self.settings = settings
        self.stop_event = stop_event or threading.Event()
        self.opener = opener or request.build_opener(request.ProxyHandler({}))
        self.metadata: dict = {}

    def _call(self, method: str, path: str, body: dict | None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = request.Request(self.settings.remote_url.rstrip('/') + path, data=data,
                              method=method,
                              headers={'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=min(10.0, max(1.0, self.settings.timeout_seconds))) as response:
            raw = response.read()
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else {'state': 'queued', 'transient': True}

    def _submit(self, body: dict) -> dict:
        try:
            return self._call('POST', '/v1/inference-jobs', body)
        except error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b'{}')
                code = payload.get('error', {}).get('code', 'server_error')
            except ValueError:
                code = 'server_error'
            raise ModelError(ERROR_MAP.get(code, 'model_unavailable')) from None
        except (error.URLError, OSError, ValueError):
            return {'state': 'queued', 'transient': True}

    def decide(self, snapshot: dict, role: str) -> Decision:
        request_id, content_hash = remote_identity(role, snapshot, self.settings)
        body = {'schema_version': 1, 'request_id': request_id, 'content_hash': content_hash,
                'backend': self.settings.remote_backend, 'model': None,
                'system': role, 'input': snapshot, 'output': 'decision-v1',
                'deadline_seconds': int(self.settings.timeout_seconds),
                'metadata': {'client': 'adviser'}}
        deadline = time.monotonic() + self.settings.timeout_seconds + 2.0
        task = self._submit(body)
        expired_once = False
        try:
            while True:
                if self.stop_event.is_set():
                    self._cancel_quietly(request_id)
                    raise ModelError('model_stopped')
                state = task.get('state')
                if state == 'succeeded':
                    result = task.get('result') or {}
                    timings = result.get('timings') or {}
                    self.metadata = {'remote_timings': timings} if timings else {}
                    return Decision.parse(result.get('decision'))
                if state == 'failed':
                    code = (task.get('error') or {}).get('code', 'server_error')
                    raise ModelError(ERROR_MAP.get(code, 'model_unavailable'))
                if state == 'cancelled':
                    raise ModelError('model_failed')
                if state == 'expired' and not expired_once:
                    expired_once = True  # Contract: expired ids rerun as new tasks.
                    task = self._submit(body)
                    continue
                if time.monotonic() >= deadline:
                    raise ModelError('model_timeout')
                self.stop_event.wait(0.2)
                task = self._poll(request_id, body)
        finally:
            self.metadata.setdefault('remote_request_id', request_id)

    def _poll(self, request_id: str, body: dict) -> dict:
        try:
            return self._call('GET', f'/v1/inference-jobs/{request_id}', None)
        except error.HTTPError as exc:
            if exc.code == 404:
                exc.read()
                return self._submit(body)  # Lost before the gateway persisted it.
            try:
                payload = json.loads(exc.read() or b'{}')
                code = payload.get('error', {}).get('code', 'server_error')
            except ValueError:
                code = 'server_error'
            raise ModelError(ERROR_MAP.get(code, 'model_unavailable')) from None
        except (error.URLError, OSError, ValueError):
            return {'state': 'queued', 'transient': True}

    def _cancel_quietly(self, request_id: str):
        try:
            self._call('POST', f'/v1/inference-jobs/{request_id}/cancel', {})
        except (error.URLError, error.HTTPError, OSError, ValueError):
            pass  # The gateway retains the task; the client stopped consuming.

    def diagnostic_metadata(self) -> dict:
        return self.metadata

    def close(self):
        return None
