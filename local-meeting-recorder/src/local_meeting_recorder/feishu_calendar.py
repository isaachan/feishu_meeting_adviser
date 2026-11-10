"""Feishu calendar reads for meeting selection (CONTRACT_STT_STREAM v2 §2.1).

Lists the authenticated user's current-window meetings from the primary
calendar and annotates whether the user is the organizer — the client-side
half of "only the organizer may record". The server still enforces the
same check; this listing is UX, not the security boundary.

Read-only stdlib client; token-invalid provider codes trigger one silent
refresh + retry through OAuthManager. Calendar/organizer field shapes
follow the calendar v4 API and need one real-API acceptance pass.
"""
from __future__ import annotations

import http.client
import json
import re
import time
from urllib import error, parse, request
from urllib.parse import quote

from .oauth import OAuthManager, _NoRedirect

BASE_URL = 'https://open.feishu.cn'
USER_INFO_PATH = '/open-apis/authen/v1/user_info'
CALENDARS_PATH = '/open-apis/calendar/v4/calendars'
EVENTS_PATH = '/open-apis/calendar/v4/calendars/{calendar_id}/events'
DEFAULT_WINDOW_SECONDS = 30 * 60
LIST_RANGE_SECONDS = 12 * 3600
_MAX_RESPONSE_BYTES = 1 << 18
_TOKEN_INVALID_CODES = frozenset({99991661, 99991663, 99991668, 99991677})
_ID_TEXT = re.compile(r'[A-Za-z0-9_.@~-]{1,256}\Z')
_EVENT_ID_TEXT = re.compile(r'[A-Za-z0-9_.@~-]{1,256}(?:/[A-Za-z0-9_.@~-]{1,256})?\Z')


class CalendarError(Exception):
    """Calendar read failed; message is safe to print."""


class _TokenRejected(Exception):
    pass


def _organizer_id(event: dict) -> str | None:
    for field in ('organizer', 'creator'):
        value = event.get(field)
        if isinstance(value, str) and _ID_TEXT.fullmatch(value):
            return value
        if isinstance(value, dict):
            for key in ('user_id', 'open_id'):
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


class MeetingCalendar:
    """Read-only calendar access with the user's OAuth token."""

    def __init__(self, oauth_manager: OAuthManager, *, base_url: str = BASE_URL,
                 timeout: float = 10.0, opener=None, clock=time.time):
        self.oauth = oauth_manager
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._open = opener or request.build_opener(_NoRedirect()).open
        self._clock = clock

    def _request(self, path: str, query: dict | None, token: str) -> dict:
        url = self.base_url + path
        if query:
            url += '?' + parse.urlencode(query)
        req = request.Request(url, method='GET', headers={
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
            raise CalendarError('feishu api unreachable') from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CalendarError('feishu api response too large')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise CalendarError('feishu api returned a non-JSON body') from None
        if not isinstance(result, dict):
            raise CalendarError('feishu api returned an unexpected body')
        code = result.get('code')
        if 200 <= status < 300 and code == 0:
            return result
        if isinstance(code, int) and code in _TOKEN_INVALID_CODES:
            raise _TokenRejected(str(code))
        raise CalendarError(f'feishu api rejected the read (http={status}, code={code})')

    def _get(self, path: str, query: dict | None = None) -> dict:
        token = self.oauth.access_token()
        try:
            return self._request(path, query, token)
        except _TokenRejected:
            token = self.oauth.access_token(rejected_token=token)
            return self._request(path, query, token)

    def user_identities(self) -> frozenset[str]:
        data = self._get(USER_INFO_PATH).get('data')
        identities = []
        if isinstance(data, dict):
            for key in ('user_id', 'open_id'):
                value = data.get(key)
                if isinstance(value, str) and _ID_TEXT.fullmatch(value):
                    identities.append(value)
        if not identities:
            raise CalendarError('feishu user info had no usable identity')
        return frozenset(identities)

    def primary_calendar_id(self) -> str | None:
        data = self._get(CALENDARS_PATH, {'page_size': 50}).get('data')
        items = data.get('calendar_list') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CalendarError('feishu calendar list had an unexpected shape')
        primary = None
        owned = None
        for item in items:
            if not isinstance(item, dict):
                continue
            calendar_id = item.get('calendar_id')
            if not isinstance(calendar_id, str) or not _ID_TEXT.fullmatch(calendar_id):
                continue
            if item.get('type') == 'primary' and primary is None:
                primary = calendar_id
            if item.get('role') == 'owner' and owned is None:
                owned = calendar_id
        return primary or owned

    def _events(self, calendar_id: str, start_ts: int, end_ts: int) -> list[dict]:
        path = EVENTS_PATH.format(calendar_id=quote(calendar_id, safe=''))
        data = self._get(path, {'start_time': start_ts, 'end_time': end_ts,
                                'page_size': 250}).get('data')
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CalendarError('feishu event list had an unexpected shape')
        return [item for item in items if isinstance(item, dict)]

    def my_meetings(self, *, window_seconds: int = DEFAULT_WINDOW_SECONDS) -> dict:
        """Meetings overlapping [now - window, now + window] on the primary
        calendar, each annotated with the organizer check result."""
        now = int(self._clock())
        identities = self.user_identities()
        calendar_id = self.primary_calendar_id()
        if calendar_id is None:
            return {'now': now, 'window_seconds': window_seconds,
                    'calendar_id': None, 'meetings': []}
        start = now - LIST_RANGE_SECONDS
        end = now + LIST_RANGE_SECONDS
        meetings = []
        for item in self._events(calendar_id, start, end):
            event_start = _event_timestamp(item, 'start_time')
            event_end = _event_timestamp(item, 'end_time')
            if event_start is None or event_end is None or event_end < event_start:
                continue  # all-day or malformed events are not selectable
            if event_end < now - window_seconds or event_start > now + window_seconds:
                continue
            raw_id = item.get('event_id')
            if not isinstance(raw_id, str) or not _EVENT_ID_TEXT.fullmatch(raw_id):
                continue
            event_id = raw_id if '/' in raw_id else f'{calendar_id}/{raw_id}'
            organizer = _organizer_id(item)
            vchat = item.get('vchat')
            meetings.append({
                'event_id': event_id,
                'summary': (item.get('summary') or '')[:120],
                'start_ts': event_start,
                'end_ts': event_end,
                'is_organizer': organizer is not None and organizer in identities,
                'online_meeting': isinstance(vchat, dict)
                                  and bool(vchat.get('meeting_url')),
            })
        meetings.sort(key=lambda meeting: (meeting['start_ts'], meeting['event_id']))
        return {'now': now, 'window_seconds': window_seconds,
                'calendar_id': calendar_id, 'meetings': meetings}
