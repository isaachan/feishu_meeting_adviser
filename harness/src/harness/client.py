"""Gateway inference client ported from the adviser RemoteModelBackend.

Implements the localhost inference contract (CONTRACT_REMOTE_INFERENCE):
idempotent submit, poll-based recovery, 404 resubmit and one expired rerun.
"""
import hashlib
import json
import time
from urllib import error, request

ROLE = 'meeting-snapshot-v1'

ERROR_HINTS = {
    'invalid_request': '请求不符合契约',
    'payload_too_large': '载荷超限',
    'content_conflict': 'request_id 内容冲突',
    'not_found': '任务丢失',
    'quota_exceeded': '网关限额已满',
    'backend_unavailable': '后端不可用',
    'execution_timeout': '执行超时',
    'output_invalid': '模型输出非法',
    'server_error': '网关内部错误',
    'cancelled': '任务被取消',
    'client_deadline': '客户端等待超时',
}


class GatewayError(Exception):
    def __init__(self, code: str):
        super().__init__(ERROR_HINTS.get(code, code))
        self.code = code


class GatewayClient:
    def __init__(self, url: str, *, backend: str = 'opencode', deadline_seconds: float = 90,
                 timeout_seconds: float = 10.0, opener=None):
        self.url = url.rstrip('/')
        self.backend = backend
        self.deadline_seconds = deadline_seconds
        self.timeout_seconds = timeout_seconds
        self.opener = opener or request.build_opener(request.ProxyHandler({}))

    def content_hash(self, snapshot: dict) -> str:
        identity = {'system': ROLE, 'input': snapshot, 'output': 'decision-v1',
                    'backend': self.backend, 'model': None}
        canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False,
                               separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def _call(self, method: str, path: str, body: dict | None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = request.Request(self.url + path, data=data, method=method,
                              headers={'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=self.timeout_seconds) as response:
            raw = response.read()
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else {'state': 'queued', 'transient': True}

    @staticmethod
    def _error_code(exc: error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read() or b'{}')
            return payload.get('error', {}).get('code', 'server_error')
        except ValueError:
            return 'server_error'

    def _submit(self, body: dict) -> dict:
        try:
            return self._call('POST', '/v1/inference-jobs', body)
        except error.HTTPError as exc:
            raise GatewayError(self._error_code(exc)) from None
        except (error.URLError, OSError, ValueError):
            return {'state': 'queued', 'transient': True}

    def _poll(self, request_id: str, body: dict) -> dict:
        try:
            return self._call('GET', f'/v1/inference-jobs/{request_id}', None)
        except error.HTTPError as exc:
            if exc.code == 404:
                exc.read()
                return self._submit(body)
            raise GatewayError(self._error_code(exc)) from None
        except (error.URLError, OSError, ValueError):
            return {'state': 'queued', 'transient': True}

    def decide(self, snapshot: dict, *, attempt: int = 0) -> dict:
        """Blocking submit + poll; returns the succeeded task dict.

        attempt > 0 suffixes request_id so a forced re-decide of identical
        content runs as a new task instead of hitting gateway idempotency;
        content_hash stays contract-canonical either way.
        """
        content_hash = self.content_hash(snapshot)
        request_id = f'rm{content_hash}' if attempt == 0 else f'rm{content_hash}-r{attempt}'
        body = {'schema_version': 1, 'request_id': request_id,
                'content_hash': content_hash, 'backend': self.backend, 'model': None,
                'system': ROLE, 'input': snapshot, 'output': 'decision-v1',
                'deadline_seconds': int(self.deadline_seconds),
                'metadata': {'client': 'llm-harness'}}
        deadline = time.monotonic() + self.deadline_seconds + 2.0
        task = self._submit(body)
        expired_once = False
        while True:
            state = task.get('state')
            if state == 'succeeded':
                return task
            if state == 'failed':
                raise GatewayError((task.get('error') or {}).get('code', 'server_error'))
            if state == 'cancelled':
                raise GatewayError('cancelled')
            if state == 'expired' and not expired_once:
                expired_once = True
                task = self._submit(body)
                continue
            if time.monotonic() >= deadline:
                raise GatewayError('client_deadline')
            time.sleep(0.2)
            task = self._poll(request_id, body)
