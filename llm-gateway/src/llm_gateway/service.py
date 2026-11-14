"""Request validation, idempotent submission and task representation."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import re

from .store import GatewayStore, REQUEUEABLE

LOG = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 4096
MAX_SYSTEM_CHARACTERS = 100_000
MAX_DEADLINE_SECONDS = 120
SUPPORTED_BACKENDS = ('fake', 'opencode')
REQUEST_ID_PATTERN = re.compile(r'[A-Za-z0-9-]{8,160}')


class RequestError(Exception):
    def __init__(self, status: int, code: str, detail: str = ''):
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    queue: int = 64
    deadline_cap: int = MAX_DEADLINE_SECONDS


def canonical_hash(system: str, input_value: dict, output: str, backend: str,
                   model: str | None, owner_open_id: str | None = None,
                   session_id: str | None = None) -> str:
    identity = {'system': system, 'input': input_value, 'output': output,
                'backend': backend, 'model': model}
    if owner_open_id is not None:
        identity['owner_open_id'] = owner_open_id
    if session_id is not None:
        identity['session_id'] = session_id
    canonical = json.dumps(identity,
                           sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def validate_request(payload: dict, *, limits: Limits) -> dict:
    if not isinstance(payload, dict):
        raise RequestError(422, 'invalid_request', 'body must be a JSON object')
    if payload.get('schema_version') != 1:
        raise RequestError(422, 'invalid_request', 'schema_version must be 1')
    request_id = payload.get('request_id')
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise RequestError(422, 'invalid_request', 'request_id must be 8..160 url-safe chars')
    backend = payload.get('backend')
    if backend not in SUPPORTED_BACKENDS:
        raise RequestError(422, 'invalid_request',
                           f'backend must be one of {"/".join(SUPPORTED_BACKENDS)}')
    model = payload.get('model')
    if model is not None and (not isinstance(model, str) or len(model) > 200):
        raise RequestError(422, 'invalid_request', 'model must be a short string or null')
    system = payload.get('system')
    if (not isinstance(system, str) or not system.strip()
            or len(system) > MAX_SYSTEM_CHARACTERS):
        raise RequestError(422, 'invalid_request', 'system must be 1..100000 characters')
    input_value = payload.get('input')
    if not isinstance(input_value, dict):
        raise RequestError(422, 'invalid_request', 'input must be a JSON object')
    if 'reference_context' in input_value:
        raise RequestError(422, 'invalid_request', 'reference_context is reserved for gateway')
    if payload.get('output') != 'decision-v1':
        raise RequestError(422, 'invalid_request', 'output must be decision-v1')
    deadline = payload.get('deadline_seconds')
    if (not isinstance(deadline, int) or isinstance(deadline, bool) or deadline < 1):
        raise RequestError(422, 'invalid_request', 'deadline_seconds must be a positive integer')
    metadata = payload.get('metadata')
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict) or len(json.dumps(metadata)) > MAX_METADATA_BYTES:
        raise RequestError(422, 'invalid_request', 'metadata must be an object up to 4 KiB')
    if '_gateway_context' in metadata:
        raise RequestError(422, 'invalid_request', 'reserved metadata')
    if len(system.encode('utf-8')) + len(json.dumps(input_value).encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise RequestError(413, 'payload_too_large', 'system+input exceeds 2 MiB')
    owner = metadata.get('owner_open_id')
    if owner is not None and (not isinstance(owner, str)
                              or not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,128}', owner)):
        raise RequestError(422, 'invalid_request', 'owner_open_id is invalid')
    session_id = metadata.get('session_id')
    if session_id is not None and (not owner or not isinstance(session_id, str)
                                   or not re.fullmatch(r's-[A-Za-z0-9_-]{1,128}', session_id)):
        raise RequestError(422, 'invalid_request', 'session_id is invalid')
    expected = canonical_hash(system, input_value, 'decision-v1', backend, model, owner, session_id)
    if payload.get('content_hash') != expected:
        raise RequestError(422, 'invalid_request', 'content_hash does not match the payload')
    return {'request_id': request_id, 'backend': backend, 'model': model, 'system': system,
            'input': input_value, 'metadata': metadata,
            'deadline_seconds': min(deadline, limits.deadline_cap)}


class GatewayService:
    def __init__(self, store: GatewayStore, *, limits: Limits | None = None, catalogs=None):
        self.store = store
        self.limits = limits or Limits()
        self.catalogs = catalogs

    def prepare(self, session_id, owner):
        if (not isinstance(session_id, str) or not re.fullmatch(r's-[A-Za-z0-9_-]{1,128}', session_id)
                or not isinstance(owner, str) or not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,128}', owner)):
            raise RequestError(422, 'invalid_request', 'invalid session identity')
        if self.catalogs is not None:
            try:
                self.catalogs.prepare(session_id, owner)
            except ValueError:
                raise RequestError(409, 'content_conflict', 'session owner conflict') from None

    def submit(self, payload: dict) -> tuple[int, dict]:
        request = validate_request(payload, limits=self.limits)
        existing = self.store.get(request['request_id'])
        if existing is not None:
            if existing['content_hash'] != payload['content_hash']:
                raise RequestError(409, 'content_conflict',
                                   'request_id already used with different content')
            if existing['state'] in REQUEUEABLE:
                self._check_queue()
                LOG.info('task requeued: request_id=%s backend=%s',
                         request['request_id'], request['backend'])
                return 202, self.represent(self.store.requeue(request['request_id']))
            LOG.info('task resubmitted (existing %s): request_id=%s',
                     existing['state'], request['request_id'])
            return 200, self.represent(existing)
        self._check_queue()
        metadata = dict(request['metadata'])
        session_id, owner = metadata.get('session_id'), metadata.get('owner_open_id')
        if session_id and owner and self.catalogs is not None:
            self.prepare(session_id, owner)
            metadata['_gateway_context'] = self.catalogs.get(session_id, owner)
        task = self.store.insert(
            request_id=request['request_id'], content_hash=payload['content_hash'],
            backend=request['backend'], model=request['model'],
            system=request['system'], input_json=json.dumps(request['input'], ensure_ascii=False),
            output='decision-v1', deadline_seconds=request['deadline_seconds'],
            metadata_json=json.dumps(metadata, ensure_ascii=False))
        LOG.info('task submitted: request_id=%s backend=%s deadline=%ss '
                 'input_chars=%s', request['request_id'], request['backend'],
                 request['deadline_seconds'], len(request['system'])
                 + len(json.dumps(request['input'], ensure_ascii=False)))
        return 202, self.represent(task)

    def _check_queue(self):
        if self.store.queued_count() >= self.limits.queue:
            raise RequestError(429, 'quota_exceeded', 'queue is full; retry later')

    def task(self, request_id: str) -> dict:
        row = self.store.get(request_id)
        if row is None:
            raise RequestError(404, 'not_found', 'unknown request_id')
        return self.represent(row)

    def cancel(self, request_id: str) -> dict:
        from .store import StorageError
        try:
            row = self.store.request_cancel(request_id)
        except StorageError:
            raise RequestError(404, 'not_found', 'unknown request_id') from None
        return self.represent(row)

    def represent(self, row: dict) -> dict:
        metadata = json.loads(row['metadata']) if row['metadata'] else {}
        metadata.pop('_gateway_context', None)
        value = {
            'request_id': row['request_id'],
            'state': row['state'],
            'created_at': _iso(row['created_ns']),
            'request': {'content_hash': row['content_hash'], 'backend': row['backend'],
                        'model': row['model'],
                        'metadata': metadata},
        }
        if row['state'] == 'succeeded' and row['result']:
            value['result'] = {
                'output': 'decision-v1',
                'decision': json.loads(row['result']),
                'timings': _timings(row),
            }
        elif row['state'] == 'failed' and row['error_code']:
            value['error'] = {'code': row['error_code']}
            value['timings'] = _timings(row)
        return value


def _timings(row: dict) -> dict | None:
    timings = {}
    if row['started_ns'] and row['created_ns']:
        timings['queued_ms'] = max(0, int((row['started_ns'] - row['created_ns']) / 1e6))
    if row.get('timings'):
        timings.update(json.loads(row['timings']))
    return timings or None


def _iso(created_ns: int) -> str:
    return datetime.fromtimestamp(created_ns / 1e9, timezone.utc).isoformat()
