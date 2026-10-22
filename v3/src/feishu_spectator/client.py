"""User-token HTTP client for spectator mode.

Two endpoints: GetUserActiveMeeting and ListMeetingEvents. Never expose
response bodies, tokens or credentials through exceptions.
"""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum
import json
import math
import re
import time
from typing import Callable, Protocol
from urllib import error, parse, request

from .models import MeetingRef

API = 'https://open.feishu.cn'
USER_ACTIVE_MEETING = '/open-apis/vc/v1/bots/user_active_meeting'
EVENTS = '/open-apis/vc/v1/bots/events'
_SAFE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


class ErrorKind(StrEnum):
    NETWORK = 'network'
    RATE_LIMIT = 'rate_limit'
    SERVER = 'server'
    TOKEN = 'token_invalid'
    DENIED = 'permission_or_presence_denied'
    PARAMETER = 'invalid_request'
    RESPONSE = 'invalid_response'
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


class TokenProvider(Protocol):
    def __call__(self, *, rejected_token: str | None = None) -> str: ...


def classify(http_status, code) -> ErrorKind:
    if code == 99991663 or http_status == 401:
        return ErrorKind.TOKEN
    if code in (99991400, 121012) or http_status == 429:
        return ErrorKind.RATE_LIMIT
    if code in (230013, 99991401, 99991662, 99991672, 99991673, 99991676, 99991679,
                120002, 120003, 121003, 121013, 121015, 121016, 121103, 121108, 121109) or http_status == 403:
        return ErrorKind.DENIED
    if code == 120001 or http_status == 400:
        return ErrorKind.PARAMETER
    if http_status in (500, 502, 503, 504):
        return ErrorKind.SERVER
    return ErrorKind.UNKNOWN


def _long_id(value) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) > 9


def parse_active_meetings(response: dict) -> list[MeetingRef]:
    """Tolerant parsing until the live response shape is verified in M3.

    Accepts data.meetings / data.meeting_list / data.items / data.meeting_infos,
    each entry either flat or nested under a `meeting` object.
    """
    data = response.get('data') if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise ApiError(ErrorKind.RESPONSE)
    entries: list = []
    for key in ('meetings', 'meeting_list', 'items', 'meeting_infos'):
        value = data.get(key)
        if isinstance(value, list):
            entries = value
            break
    else:
        single = data.get('meeting')
        entries = [single] if isinstance(single, dict) else []
    meetings = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ApiError(ErrorKind.RESPONSE)
        source = entry.get('meeting') if isinstance(entry.get('meeting'), dict) else entry
        meeting_id = source.get('meeting_id') or source.get('id')
        if not _long_id(meeting_id):
            raise ApiError(ErrorKind.RESPONSE)
        meeting_no = source.get('meeting_no')
        topic = source.get('meeting_title') or source.get('topic') or source.get('title')
        meetings.append(MeetingRef(
            meeting_id=meeting_id,
            meeting_no=meeting_no if isinstance(meeting_no, str) else '',
            topic=topic if isinstance(topic, str) else '',
        ))
    return meetings


@dataclass(frozen=True, repr=False)
class Credentials:
    app_id: str
    app_secret: str


class SpectatorClient:
    def __init__(self, token_provider: TokenProvider, *, timeout=10, request_seconds=0.7,
                 opener=request.urlopen, monotonic=time.monotonic, clock=time.time, wait=None,
                 max_response_bytes=4_000_000):
        if (not math.isfinite(request_seconds) or request_seconds < 0.7
                or not 1 <= timeout <= 60):
            raise ValueError('invalid HTTP timeout or request interval')
        self.token_provider = token_provider
        self.timeout = timeout
        self.request_seconds = request_seconds
        self.opener = opener
        self.monotonic = monotonic
        self.clock = clock
        self.wait = wait or (lambda seconds: (time.sleep(seconds), False)[1])
        self.max_response_bytes = max_response_bytes
        self._last_request = None

    def _rate_limit(self):
        if self.wait(0):
            raise StopRequested()
        if self._last_request is not None:
            delay = self.request_seconds - (self.monotonic() - self._last_request)
            if delay > 0 and self.wait(delay):
                raise StopRequested()
        self._last_request = self.monotonic()

    def _get(self, path: str, *, query: dict | None = None, timeout=None, _replayed=False):
        if path not in (USER_ACTIVE_MEETING, EVENTS):
            raise ValueError('unsupported API endpoint')
        url = API + path + ('?' + parse.urlencode(query) if query else '')
        token = self.token_provider(rejected_token=getattr(self, '_last_token', None)
                                    if _replayed else None)
        req = request.Request(url, headers={'Accept': 'application/json', 'Authorization': 'Bearer ' + token},
                              method='GET')
        self._rate_limit()
        try:
            request_timeout = self.timeout if timeout is None else min(self.timeout, max(0.01, timeout))
            response = self.opener(req, timeout=request_timeout)
        except error.HTTPError as exc:
            response = exc
        except (error.URLError, TimeoutError, OSError):
            raise ApiError(ErrorKind.NETWORK) from None
        try:
            with response:
                status = getattr(response, 'status', getattr(response, 'code', 200))
                response_headers = response.headers
                raw = response.read(self.max_response_bytes + 1)
        except (error.URLError, TimeoutError, OSError):
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
        for key in ('X-Request-Id', 'X-Tt-Logid'):
            value = response_headers.get(key)
            if isinstance(value, str) and _SAFE_ID.fullmatch(value) and token not in value:
                ids[key.lower()] = value
        if isinstance(data, dict):
            for key in ('request_id', 'log_id'):
                value = data.get(key)
                if isinstance(value, str) and _SAFE_ID.fullmatch(value) and token not in value:
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
            api_error = ApiError(classify(status, code), http_status=status, code=code,
                                 request_ids=ids, retry_after=retry_after)
            # One provider refresh and replay after an explicit authentication rejection.
            if api_error.kind is ErrorKind.TOKEN and not _replayed:
                self._last_token = token
                return self._get(path, query=query, timeout=timeout, _replayed=True)
            raise api_error
        if not isinstance(data, dict) or code != 0:
            raise ApiError(ErrorKind.RESPONSE, http_status=status, request_ids=ids)
        self._last_token = None
        return data

    def active_meetings(self) -> list[MeetingRef]:
        return parse_active_meetings(self._get(USER_ACTIVE_MEETING))

    def events(self, query: dict) -> dict:
        if not isinstance(query, dict) or not query.get('meeting_id'):
            raise ValueError('events query requires meeting_id')
        return self._get(EVENTS, query=query)
