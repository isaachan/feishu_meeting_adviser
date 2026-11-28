"""Auth-node client and in-memory admin sessions (stdlib only).

The admin site never handles Feishu tokens: it starts a discovery login on the
auth node, then reads the identity from ``/authorize/status`` and keeps its own
opaque session cookie in memory.
"""
import json
import secrets
import threading
import time
from urllib import error, parse, request

ADMIN_LOGIN_INTENT = 'admin:login'
STATE_TTL_S = 600


class AuthNodeError(Exception):
    def __init__(self, message, *, status=None):
        self.status = status
        super().__init__(message)


class AuthClient:
    def __init__(self, base_url='http://127.0.0.1:8381', *, timeout=10, opener=None):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = opener or request.build_opener(request.ProxyHandler({})).open

    def _call(self, method: str, path: str, body=None) -> dict:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = request.Request(self.base_url + path, data=data, method=method,
                              headers=headers)
        try:
            with self.opener(req, timeout=self.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            try:
                exc.read()
            finally:
                exc.close()
            raise AuthNodeError('auth node rejected request',
                                status=exc.code) from None
        except (error.URLError, OSError) as exc:
            raise AuthNodeError(exc.__class__.__name__) from exc
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise AuthNodeError('invalid json payload') from None
        if not isinstance(payload, dict):
            raise AuthNodeError('unexpected payload shape')
        return payload

    def authorize_start(self) -> dict:
        result = self._call('POST', '/authorize/start',
                            {'open_id': None, 'intent': ADMIN_LOGIN_INTENT})
        url = result.get('authorize_url')
        state = result.get('state')
        if not isinstance(url, str) or not url or not isinstance(state, str) or not state:
            raise AuthNodeError('authorize_start reply missing url/state')
        return result

    def authorize_status(self, state) -> dict:
        try:
            return self._call('GET', '/authorize/status?state=' + parse.quote(state, safe=''))
        except AuthNodeError as exc:
            if exc.status == 404:
                return {'status': 'expired'}
            raise


class AdminSessions:
    """Opaque session tokens; absolute TTL, lost on process restart."""

    def __init__(self, ttl_s=28800, *, clock=time.time):
        self.ttl_s = ttl_s
        self.clock = clock
        self._lock = threading.Lock()
        self._items = {}

    def issue(self, open_id: str, user_id: str, name=None) -> str:
        token = 'as-' + secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._prune(now)
            self._items[token] = {'open_id': open_id, 'user_id': user_id,
                                  'name': name, 'expires_at': now + self.ttl_s}
        return token

    def get(self, token) -> dict | None:
        if not isinstance(token, str) or not token:
            return None
        now = self.clock()
        with self._lock:
            self._prune(now)
            item = self._items.get(token)
            return dict(item) if item is not None else None

    def revoke(self, token):
        with self._lock:
            self._items.pop(token, None)

    def _prune(self, now):
        for token in [key for key, value in self._items.items()
                      if value['expires_at'] <= now]:
            del self._items[token]
