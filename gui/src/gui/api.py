"""Advisor GUI-face HTTP client (CONTRACT_ADVISOR_API §8).

stdlib-only (headless-testable). Never logs headers or bodies: requests carry
the device_token and login URLs.
"""
import json
from urllib import error, parse, request


class ApiError(Exception):
    """Non-2xx reply; ``code`` follows the contract (bad_token/expired/...)."""

    def __init__(self, status, code, message=''):
        self.status, self.code, self.message = status, code, message
        super().__init__(f'{status} {code}: {message}')


class NetworkError(Exception):
    """Transport-level failure (offline, timeout, malformed reply)."""


class GuiApiClient:
    def __init__(self, base_url, *, timeout=10.0, opener=None):
        if not isinstance(base_url, str):
            raise ValueError('base_url required')
        value = base_url.strip().rstrip('/')
        if not value.startswith(('http://', 'https://')):
            raise ValueError('server url must start with http:// or https://')
        self.base_url = value
        self.timeout = timeout
        self._open = opener or request.build_opener(request.ProxyHandler({})).open

    def _call(self, method, path, *, body=None, token=None):
        headers = {'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        req = request.Request(self.base_url + path, data=data, method=method,
                              headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            raise _http_error(exc) from None
        except (error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(str(exc)) from exc
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise NetworkError('invalid json payload') from None
        if not isinstance(payload, dict):
            raise NetworkError('unexpected payload shape')
        return payload

    def login_start(self):
        return self._call('POST', '/gui/login/start', body={})

    def login_poll(self, device_code):
        return self._call('GET', '/gui/login/poll?device_code=' + parse.quote(device_code))

    def sessions(self, device_token):
        return self._call('GET', '/gui/sessions', token=device_token)

    def logout(self, device_token):
        return self._call('POST', '/gui/logout', body={}, token=device_token)


def _http_error(exc):
    try:
        payload = json.loads(exc.read() or b'{}')
    except (ValueError, UnicodeDecodeError):
        payload = {}
    detail = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        detail = {}
    return ApiError(exc.code, detail.get('code', 'internal'), detail.get('message', ''))
