"""Feishu organizer authorization per CONTRACT_STT_STREAM v2 (`auth: feishu`).

Validated once per connection, before the session slot and recognizer are
created: the user_access_token must resolve to a user identity, the
calendar event must exist and be visible, its organizer must be that user,
and the current time must be inside [start - 30 min, end + 30 min].
Successful validations are cached briefly so the mic/loopback dual
connections of one user do not call Feishu twice.

Failure mapping is fail-closed: unreachable or garbled upstreams map to
``auth_unavailable``; rejected tokens map to ``auth_expired``; business
rejections on the event read (not visible / not found) and organizer or
window mismatches map to ``unauthorized``.

The HTTP client is stdlib-only. Compound calendar event ids
(``<calendar_id>/<event_id>``) come from Feishu list endpoints; shapes were
verified against the production API on 2026-11-10 (event_organizer.user_id
carrying an open_id; user_info returning open_id/union_id only; code 20005
for rejected tokens) - see data/acceptance-auth/.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib import error, request
from urllib.parse import quote

BASE_URL = 'https://open.feishu.cn'
USER_INFO_PATH = '/open-apis/authen/v1/user_info'
EVENT_PATH = '/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}'
WINDOW_SECONDS = 30 * 60
DEFAULT_CACHE_SECONDS = 10 * 60
_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1 << 18
_TOKEN_TEXT = re.compile(r'[A-Za-z0-9_.=-]{1,4096}\Z')
_ID_TEXT = re.compile(r'[A-Za-z0-9_.@~-]{1,256}\Z')
# Provider codes meaning the presented user token is invalid or expired
# (20005 = invalid access token on authen v1 user_info, seen in production).
_TOKEN_INVALID_CODES = frozenset({20005, 99991661, 99991663, 99991668,
                                   99991677})


class AuthError(Exception):
    """Validation refused; ``code`` is a contract error code (§3.5)."""

    def __init__(self, code: str, message: str):
        if code not in ('unauthorized', 'auth_expired', 'auth_unavailable'):
            code = 'auth_unavailable'
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ValidatedAuth:
    user_id: str
    calendar_event_id: str


class AuthValidate(Protocol):
    """What SttService needs from a validator (auth: feishu)."""

    def validate(self, token, calendar_event_id) -> ValidatedAuth: ...


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FeishuAuthTransport:
    """Read-only Feishu calls with the caller's user_access_token.

    Never sends application credentials; tokens only travel in the
    Authorization header over HTTPS to the configured base URL.
    """

    def __init__(self, *, base_url: str = BASE_URL, timeout: float = _TIMEOUT_SECONDS,
                 opener=None):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._open = opener or request.build_opener(_NoRedirect()).open

    def _get(self, path: str, token: str) -> tuple[int, dict | None]:
        req = request.Request(self.base_url + path, method='GET', headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        try:
            try:
                response = self._open(req, timeout=self.timeout)
            except error.HTTPError as exc:
                response = exc
            with response:
                status = getattr(response, 'status', getattr(response, 'code', 200))
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise AuthError('auth_unavailable', 'feishu api unreachable') from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AuthError('auth_unavailable', 'feishu api response too large')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            result = None
        return status, result if isinstance(result, dict) else None

    def user_info(self, token: str) -> tuple[str, frozenset[str]]:
        """Return (primary id, all ids) for the token's user."""
        status, result = self._get(USER_INFO_PATH, token)
        code = result.get('code') if result else None
        if code == 0:
            data = result.get('data') if isinstance(result, dict) else None
            identities: list[str] = []
            if isinstance(data, dict):
                for key in ('user_id', 'open_id', 'union_id'):
                    value = data.get(key)
                    if isinstance(value, str) and _ID_TEXT.fullmatch(value):
                        identities.append(value)
            if identities:
                return identities[0], frozenset(identities)
            raise AuthError('auth_unavailable', 'feishu user info had no usable identity')
        if isinstance(code, int) and code in _TOKEN_INVALID_CODES:
            raise AuthError('auth_expired', 'user access token was rejected')
        raise AuthError('auth_unavailable',
                        f'feishu user info failed (http={status}, code={code})')

    def calendar_event(self, token: str, calendar_id: str, event_id: str) -> dict:
        """Return the raw event object; caller checks organizer and times."""
        path = EVENT_PATH.format(calendar_id=quote(calendar_id, safe=''),
                                 event_id=quote(event_id, safe=''))
        status, result = self._get(path, token)
        code = result.get('code') if result else None
        if code == 0:
            data = result.get('data') if isinstance(result, dict) else None
            event = data.get('event') if isinstance(data, dict) else None
            if isinstance(event, dict):
                return event
            raise AuthError('auth_unavailable', 'feishu event response had no event body')
        if isinstance(code, int) and code in _TOKEN_INVALID_CODES:
            raise AuthError('auth_expired', 'user access token was rejected')
        # A business rejection on the event read (no access / not found for
        # this user) is an authorization failure, not an outage.
        raise AuthError('unauthorized', 'calendar event is not available to this user')


def _organizer_id(event: dict) -> str | None:
    # Real shapes (2026-11-10 probe): events carry ``event_organizer`` whose
    # ``user_id`` key holds an open_id-domain value; ``organizer``/``creator``
    # remain as tolerant fallbacks.
    for field in ('event_organizer', 'organizer', 'creator'):
        value = event.get(field)
        if isinstance(value, str) and _ID_TEXT.fullmatch(value):
            return value
        if isinstance(value, dict):
            for key in ('user_id', 'open_id', 'union_id'):
                candidate = value.get(key)
                if isinstance(candidate, str) and _ID_TEXT.fullmatch(candidate):
                    return candidate
    return None


def _event_timestamp(event: dict, key: str) -> int | None:
    value = event.get(key)
    if not isinstance(value, dict):
        return None
    timestamp = value.get('timestamp')
    if isinstance(timestamp, bool):
        return None
    if isinstance(timestamp, int):
        return timestamp
    if isinstance(timestamp, str) and timestamp.isdigit() and len(timestamp) <= 12:
        return int(timestamp)
    return None


class AuthValidator:
    """Organizer validation with a short success cache (contract §1)."""

    def __init__(self, *, transport=None, window_seconds: int = WINDOW_SECONDS,
                 cache_seconds: float = DEFAULT_CACHE_SECONDS, clock=time.time):
        self.transport = transport or FeishuAuthTransport()
        self.window_seconds = window_seconds
        self.cache_seconds = cache_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, ValidatedAuth]] = {}

    def validate(self, token, calendar_event_id) -> ValidatedAuth:
        if not isinstance(token, str) or not _TOKEN_TEXT.fullmatch(token):
            raise AuthError('auth_expired', 'user access token is missing or malformed')
        if not isinstance(calendar_event_id, str) or len(calendar_event_id) > 513:
            raise AuthError('unauthorized', 'invalid calendar event id')
        parts = calendar_event_id.rsplit('/', 1)
        if len(parts) != 2 or not all(_ID_TEXT.fullmatch(part) for part in parts):
            raise AuthError('unauthorized', 'invalid calendar event id')
        key = (token, calendar_event_id)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and now < hit[0]:
            return hit[1]
        primary_id, identities = self.transport.user_info(token)
        event = self.transport.calendar_event(token, parts[0], parts[1])
        organizer = _organizer_id(event)
        if organizer is None or organizer not in identities:
            raise AuthError('unauthorized',
                            'user is not the organizer of the selected meeting')
        start_ts = _event_timestamp(event, 'start_time')
        end_ts = _event_timestamp(event, 'end_time')
        if start_ts is None or end_ts is None or end_ts < start_ts:
            raise AuthError('unauthorized', 'selected meeting has no usable time window')
        now = self._clock()
        if not start_ts - self.window_seconds <= now <= end_ts + self.window_seconds:
            raise AuthError('unauthorized',
                            'current time is outside the meeting time window')
        validated = ValidatedAuth(user_id=primary_id,
                                  calendar_event_id=calendar_event_id)
        with self._lock:
            self._cache[key] = (now + self.cache_seconds, validated)
            for stale in [k for k, (expiry, _) in self._cache.items() if expiry <= now]:
                del self._cache[stale]
        return validated
