"""Advisor admin-face client per CONTRACT_ADVISOR_API §9 (admin-site side).

Request plumbing copied from bot's advisor_client (repo root, 2026-11-13)
and trimmed to the admin face.
"""
import json
from urllib import error, request
from uuid import uuid4


class AdvisorApiError(Exception):
    def __init__(self, status, code, message, payload=None):
        self.status, self.code, self.message = status, code, message
        self.payload = payload if payload is not None else {
            'error': {'code': code, 'message': message}}
        super().__init__(f'{status} {code}')


class AdvisorAdminClient:
    def __init__(self, base_url='http://127.0.0.1:8380', *, timeout=10, opener=None):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = opener or request.build_opener(request.ProxyHandler({})).open

    def _call(self, method, path, payload=None, *, idempotency_key=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        if idempotency_key:
            headers['X-Idempotency-Key'] = idempotency_key
        req = request.Request(self.base_url + path, data=data, method=method,
                              headers=headers)
        try:
            with self.opener(req, timeout=self.timeout) as response:
                raw = response.read()
            return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b'{}')
            except ValueError:
                payload = {}
            details = payload.get('error', {})
            raise AdvisorApiError(exc.code, details.get('code', 'internal'),
                                  details.get('message', ''), payload) from None
        except (error.URLError, OSError, ValueError) as exc:
            raise AdvisorApiError(502, 'advisor_unreachable',
                                  exc.__class__.__name__) from exc

    def admin_sessions(self):
        return self._call('GET', '/admin/sessions')

    def admin_end_session(self, session_id):
        return self._call('POST', f'/admin/sessions/{session_id}/end')

    def admin_supervisors(self):
        return self._call('GET', '/admin/supervisors')

    def admin_put_supervisor(self, login_name, enabled):
        # Fresh key per logical request (contract §"所有 POST 必须携带"): retries
        # of one click dedup server-side; a later toggle is a new key, never a
        # replay of the previous response.
        return self._call('POST', '/admin/supervisors',
                          {'login_name': login_name, 'enabled': enabled},
                          idempotency_key=uuid4().hex)

    def admin_get_config(self):
        return self._call('GET', '/admin/config')

    def admin_put_config(self, idle_timeout_s):
        # Same fresh-key rationale as admin_put_supervisor.
        return self._call('POST', '/admin/config',
                          {'idle_timeout_s': idle_timeout_s},
                          idempotency_key=uuid4().hex)
