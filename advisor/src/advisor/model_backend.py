"""Model decision protocol and the gateway RemoteModelBackend (ported).

Decision surface is plan-C trimmed: control is none|cancel only (pause/resume
participation was removed). RemoteModelBackend implements the localhost
inference contract (CONTRACT_REMOTE_INFERENCE) with idempotent submit and
poll-based recovery, exactly as the legacy advisor implementation.
"""
from dataclasses import dataclass
import hashlib
import json
import logging
import threading
import time
from urllib import error, request

from .errors import ModelError
from .settings import AdviserSettings

LOG = logging.getLogger(__name__)

DECISION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'control': {'type': 'string', 'enum': ['none', 'cancel']},
        'control_sentence_keys': {'type': 'array', 'items': {'type': 'string'}},
        'reply': {'type': ['string', 'null']},
        'reply_sentence_keys': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['control', 'control_sentence_keys', 'reply', 'reply_sentence_keys'],
}


@dataclass(frozen=True)
class Decision:
    control: str
    control_sentence_keys: tuple[str, ...]
    reply: str | None
    reply_sentence_keys: tuple[str, ...]

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != set(DECISION_SCHEMA['required']):
            raise ModelError('model_invalid_decision')
        control, reply = value['control'], value['reply']
        if not isinstance(control, str) or control not in ('none', 'cancel'):
            raise ModelError('model_invalid_decision')
        for key in ('control_sentence_keys', 'reply_sentence_keys'):
            items = value[key]
            if not isinstance(items, list) or len(items) > 100 or any(
                    not isinstance(x, str) or not x or len(x) > 256 for x in items):
                raise ModelError('model_invalid_decision')
            if len(items) != len(set(items)):
                raise ModelError('model_invalid_decision')
        if reply is not None and (not isinstance(reply, str) or not reply.strip()
                                  or len(reply.encode('utf-8')) > 4000):
            raise ModelError('model_invalid_decision')
        if (control == 'none') != (not value['control_sentence_keys']):
            raise ModelError('model_invalid_decision')
        if (reply is None) != (not value['reply_sentence_keys']):
            raise ModelError('model_invalid_decision')
        if control == 'cancel' and reply is not None:
            raise ModelError('model_invalid_decision')
        return cls(control, tuple(value['control_sentence_keys']), reply,
                   tuple(value['reply_sentence_keys']))

    def to_dict(self):
        return {'control': self.control, 'control_sentence_keys': list(self.control_sentence_keys),
                'reply': self.reply, 'reply_sentence_keys': list(self.reply_sentence_keys)}


def load_role(settings: AdviserSettings) -> tuple[str, str]:
    try:
        if settings.role_file:
            path = settings.role_file
            if path.is_symlink() or not path.is_file():
                raise ModelError('model_role_unavailable')
            raw = path.read_bytes()
        else:
            from importlib.resources import files
            raw = files('advisor').joinpath('meeting_adviser.md').read_bytes()
        if not 1 <= len(raw) <= 100000:
            raise ModelError('model_role_unavailable')
        role = raw.decode('utf-8')
        if not role.strip():
            raise ModelError('model_role_unavailable')
        import hashlib as _hashlib
        return role, _hashlib.sha256(role.encode('utf-8')).hexdigest()
    except (OSError, UnicodeDecodeError):
        raise ModelError('model_role_unavailable') from None


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


def remote_identity(system: str, input_value: dict, settings: AdviserSettings,
                    owner_open_id: str | None = None) -> tuple[str, str]:
    identity = {'system': system, 'input': input_value,
                'output': 'decision-v1', 'backend': settings.remote_backend, 'model': None}
    if owner_open_id is not None:
        identity['owner_open_id'] = owner_open_id
    canonical = json.dumps(identity,
                           sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    content_hash = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return 'rm' + content_hash, content_hash


class RemoteModelBackend:
    """Blocking decision call with idempotent submit and poll-based recovery."""

    context_overhead_characters = 0

    def __init__(self, settings: AdviserSettings, *, stop_event=None, opener=None):
        self.settings = settings
        self.stop_event = stop_event or threading.Event()
        self.opener = opener or request.build_opener(request.ProxyHandler({}))
        self.metadata: dict = {}
        self.owner_open_id: str | None = None

    def _call(self, method: str, path: str, body: dict | None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = request.Request(self.settings.remote_url.rstrip('/') + path, data=data,
                              method=method,
                              headers={'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=min(10.0, max(1.0, self.settings.model_timeout_seconds))) as response:
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
        request_id, content_hash = remote_identity(role, snapshot, self.settings,
                                                   self.owner_open_id)
        metadata = {'client': 'adviser'}
        if self.owner_open_id is not None:
            metadata['owner_open_id'] = self.owner_open_id
        body = {'schema_version': 1, 'request_id': request_id, 'content_hash': content_hash,
                'backend': self.settings.remote_backend, 'model': None,
                'system': role, 'input': snapshot, 'output': 'decision-v1',
                'deadline_seconds': int(self.settings.model_timeout_seconds),
                'metadata': metadata}
        deadline = time.monotonic() + self.settings.model_timeout_seconds + 2.0
        started = time.monotonic()
        task = self._submit(body)
        expired_once = False
        LOG.info('gateway submit: request_id=%s backend=%s state=%s',
                 request_id, self.settings.remote_backend, task.get('state'))
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
                    LOG.info('gateway done: request_id=%s elapsed_ms=%.0f timings=%s',
                             request_id, (time.monotonic() - started) * 1000, timings)
                    return Decision.parse(result.get('decision'))
                if state == 'failed':
                    code = (task.get('error') or {}).get('code', 'server_error')
                    LOG.warning('gateway failed: request_id=%s code=%s', request_id, code)
                    raise ModelError(ERROR_MAP.get(code, 'model_unavailable'))
                if state == 'cancelled':
                    LOG.warning('gateway cancelled: request_id=%s', request_id)
                    raise ModelError('model_failed')
                if state == 'expired' and not expired_once:
                    expired_once = True  # Contract: expired ids rerun as new tasks.
                    LOG.warning('gateway expired, resubmitting: request_id=%s', request_id)
                    task = self._submit(body)
                    continue
                if time.monotonic() >= deadline:
                    LOG.warning('gateway deadline exceeded: request_id=%s', request_id)
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
