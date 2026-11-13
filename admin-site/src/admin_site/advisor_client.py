"""Advisor admin-face client per CONTRACT_ADVISOR_API §9 (admin-site side).

Request plumbing copied from bot's advisor_client (repo root, 2026-11-13)
and trimmed to the admin face.
"""
import json
from urllib import error, request


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

    def _call(self, method, path):
        req = request.Request(self.base_url + path, data=b'' if method == 'POST' else None,
                              method=method)
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
