"""Structured Feishu HTTP errors and bounded authentication refresh.

Never expose response msg, request parameters, raw body, token or credentials.
"""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum
import json
from http.client import HTTPException
import math
import re
import time
from urllib import error, parse, request

API = 'https://open.feishu.cn'
TOKEN = '/open-apis/auth/v3/tenant_access_token/internal'
EVENTS = '/open-apis/vc/v1/bots/events'
JOIN = '/open-apis/vc/v1/bots/join'
LEAVE = '/open-apis/vc/v1/bots/leave'


class ErrorKind(StrEnum):
    NETWORK = 'network'
    RATE_LIMIT = 'rate_limit'
    SERVER = 'server'
    TOKEN = 'token_invalid'
    DENIED = 'permission_or_admission_denied'
    BOT_ABSENT = 'bot_not_in_meeting'
    PARAMETER = 'invalid_request'
    RESPONSE = 'invalid_response'
    ACTION_REQUIRED = 'service_requires_manual_action'
    UNKNOWN = 'unknown_api_error'


class ApiError(Exception):
    def __init__(self, kind: ErrorKind, *, http_status=None, code=None, request_ids=None, retry_after=None):
        self.kind = kind
        self.http_status = http_status
        self.code = code
        self.request_ids = request_ids or {}
        self.retry_after = retry_after
        super().__init__(f'{kind.value} (HTTP={http_status}, code={code}); server details hidden')

    @property
    def recoverable(self) -> bool:
        return self.kind in (ErrorKind.NETWORK, ErrorKind.RATE_LIMIT, ErrorKind.SERVER)

    def safe_details(self) -> dict:
        return {'error_kind': self.kind.value, 'http_status': self.http_status,
                'code': self.code, 'request_ids': self.request_ids, 'retry_after': self.retry_after}


class StopRequested(Exception):
    pass


@dataclass(frozen=True, repr=False)
class Credentials:
    app_id: str
    app_secret: str


_SAFE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def classify(http_status, code) -> ErrorKind:
    if code in (121001, 121101):
        return ErrorKind.ACTION_REQUIRED  # Endpoint docs explicitly discourage sustained retries.
    if code in (120004, 121018, 121110):
        return ErrorKind.BOT_ABSENT
    if code == 99991663 or http_status == 401:
        return ErrorKind.TOKEN
    if code in (99991400, 121012) or http_status == 429:
        return ErrorKind.RATE_LIMIT
    if code in (99991401, 99991662, 99991672, 99991673, 99991676, 99991679, 120002, 120003,
                121003, 121013, 121015, 121016, 121103, 121108, 121109) or http_status == 403:
        return ErrorKind.DENIED
    if code == 120001 or http_status == 400:
        return ErrorKind.PARAMETER
    if http_status in (500, 502, 503, 504):
        return ErrorKind.SERVER
    return ErrorKind.UNKNOWN


class FeishuClient:
    def __init__(self, credentials: Credentials, *, timeout=10, request_seconds=1, opener=request.urlopen,
                 monotonic=time.monotonic, clock=time.time, wait=None, max_response_bytes=4_000_000):
        if not credentials.app_id or not credentials.app_secret:
            raise ValueError('application credentials required')
        if (not math.isfinite(request_seconds) or request_seconds < 0.7 or not 1 <= timeout <= 60):
            raise ValueError('invalid HTTP timeout or request interval')
        self.credentials = credentials
        self.timeout = timeout
        self.request_seconds = request_seconds
        self.opener = opener
        self.monotonic = monotonic
        self.clock = clock
        self.wait = wait or (lambda seconds: (time.sleep(seconds), False)[1])
        self.max_response_bytes = max_response_bytes
        self._token = None
        self._expires_at = 0.0
        self._refresh_margin = 0.0
        self._last_request = None

    def _rate_limit(self):
        if self._last_request is not None:
            delay = self.request_seconds - (self.monotonic() - self._last_request)
            if delay > 0 and self.wait(delay):
                raise StopRequested()
        self._last_request = self.monotonic()

    def _request(self, path, *, payload=None, query=None, token=None):
        if path not in (TOKEN, EVENTS, JOIN, LEAVE):
            raise ValueError('unsupported API endpoint')
        url = API + path + ('?' + parse.urlencode(query) if query else '')
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        body = json.dumps(payload).encode() if payload is not None else None
        if body is not None:
            headers['Content-Type'] = 'application/json; charset=utf-8'
        req = request.Request(url, data=body, headers=headers, method='POST' if body is not None else 'GET')
        self._rate_limit()
        try:
            response = self.opener(req, timeout=self.timeout)
        except error.HTTPError as exc:
            response = exc
        except (error.URLError, TimeoutError, OSError, HTTPException):
            raise ApiError(ErrorKind.NETWORK) from None
        try:
            with response:
                status = getattr(response, 'status', getattr(response, 'code', 200))
                response_headers = response.headers
                raw = response.read(self.max_response_bytes + 1)
        except (error.URLError, TimeoutError, OSError, HTTPException):
            raise ApiError(ErrorKind.NETWORK) from None
        data = None
        if len(raw) <= self.max_response_bytes:
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                pass
        code = data.get('code') if isinstance(data, dict) else None
        if type(code) is not int:
            code = None
        ids = {}
        secrets = (self.credentials.app_id, self.credentials.app_secret, token)
        for key in ('X-Request-Id', 'X-Tt-Logid'):
            value = response_headers.get(key)
            if isinstance(value, str) and _SAFE_ID.fullmatch(value) and not any(s and s in value for s in secrets):
                ids[key.lower()] = value
        if isinstance(data, dict):
            for key in ('request_id', 'log_id'):
                value = data.get(key)
                if isinstance(value, str) and _SAFE_ID.fullmatch(value) and not any(s and s in value for s in secrets):
                    ids[key] = value
        retry_after = None
        raw_retry = response_headers.get('Retry-After')
        if raw_retry:
            try:
                value = float(raw_retry)
            except ValueError:
                try:
                    value = parsedate_to_datetime(raw_retry).timestamp() - self.clock()
                except (ValueError, TypeError, OverflowError):
                    value = 0
            if math.isfinite(value):
                retry_after = max(0, min(3600, value))
        if not 200 <= status < 300 or code not in (None, 0):
            raise ApiError(classify(status, code), http_status=status, code=code, request_ids=ids, retry_after=retry_after)
        if not isinstance(data, dict) or code != 0:
            raise ApiError(ErrorKind.RESPONSE, http_status=status, request_ids=ids)
        return data

    def _refresh(self):
        result = self._request(TOKEN, payload={'app_id': self.credentials.app_id, 'app_secret': self.credentials.app_secret})
        token = result.get('tenant_access_token')
        expiry = result.get('expire')
        if not isinstance(token, str) or not token or type(expiry) is not int or not 1 <= expiry <= 7200:
            raise ApiError(ErrorKind.RESPONSE)
        self._token = token
        self._expires_at = self.monotonic() + expiry
        self._refresh_margin = min(300, expiry / 2)

    def authenticated(self, path, *, payload=None, query=None):
        if self._token is None or self.monotonic() >= self._expires_at - self._refresh_margin:
            self._refresh()
        try:
            return self._request(path, payload=payload, query=query, token=self._token)
        except ApiError as exc:
            if exc.kind != ErrorKind.TOKEN:
                raise
        # One refresh, one replay after an explicit authentication rejection. No network replay for mutations.
        self._token = None
        self._refresh()
        return self._request(path, payload=payload, query=query, token=self._token)

    def events(self, query: dict) -> dict:
        return self.authenticated(EVENTS, query=query)

    def join(self, meeting_no: str, password: str = '') -> dict:
        payload = {'join_type': 1, 'join_identify': {'meeting_no': meeting_no}}
        if password:
            payload['password'] = password
        return self.authenticated(JOIN, payload=payload)

    def leave(self, meeting_id: str) -> dict:
        return self.authenticated(LEAVE, payload={'meeting_id': meeting_id})
