"""Real calendar validation (plan-1 asset map: feishu_calendar.py ported to
the advisor; meeting_no <-> event matching via vchat.meeting_url).

Checks per REQUIREMENTS S1: the meeting exists, the requesting user is the
event organizer, and now is within [start-30min, end+30min]. The event
description becomes the session material (plan-C decision).
"""
import json
import re
import time
from urllib import error, parse, request

from .api import CalendarError
from .auth_client import AuthUnavailable, AuthorizationRequired, HttpAuthClient

BASE_URL = 'https://open.feishu.cn'
USER_INFO_PATH = '/open-apis/authen/v1/user_info'
CALENDARS_PATH = '/open-apis/calendar/v4/calendars'
EVENTS_PATH = '/open-apis/calendar/v4/calendars/{calendar_id}/events'
LIST_RANGE_SECONDS = 12 * 3600
WINDOW_SECONDS = 30 * 60
MAX_RESPONSE_BYTES = 1 << 18
TOKEN_INVALID_CODES = frozenset({20005, 99991661, 99991663, 99991668, 99991677})
ID_TEXT = re.compile(r'[A-Za-z0-9_.@~-]{1,256}\Z')
MEETING_NO_IN_URL = re.compile(r'/j/([0-9]{9})')


def _organizer_id(event: dict) -> str | None:
    # Real shapes (2026-11-10 probe): event_organizer.user_id is open_id-domain.
    for field in ('event_organizer', 'organizer', 'creator'):
        value = event.get(field)
        if isinstance(value, str) and ID_TEXT.fullmatch(value):
            return value
        if isinstance(value, dict):
            for key in ('user_id', 'open_id', 'union_id'):
                candidate = value.get(key)
                if isinstance(candidate, str) and ID_TEXT.fullmatch(candidate):
                    return candidate
    return None


def _timestamp(event: dict, key: str) -> int | None:
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


class FeishuCalendarValidator:
    """CalendarValidator implementation backed by the user's OAuth token."""

    def __init__(self, auth: HttpAuthClient, *, base_url=BASE_URL, timeout=10.0,
                 opener=None, clock=time.time):
        self.auth = auth
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._open = opener or request.build_opener(request.ProxyHandler({})).open
        self._clock = clock

    # -- HTTP ---------------------------------------------------------------

    def _request(self, path, query, token):
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
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (error.URLError, TimeoutError, OSError):
            raise CalendarError('auth_unavailable') from None
        try:
            result = json.loads(raw[:MAX_RESPONSE_BYTES])
        except (ValueError, UnicodeDecodeError):
            raise CalendarError('auth_unavailable') from None
        code = result.get('code')
        if 200 <= status < 300 and code == 0:
            return result
        if isinstance(code, int) and code in TOKEN_INVALID_CODES:
            raise _TokenRejected(code)
        raise CalendarError('auth_unavailable')

    def _get(self, path, query, owner_open_id):
        try:
            token = self.auth.get_token(owner_open_id)['access_token']
            try:
                return self._request(path, query, token)
            except _TokenRejected:
                token = self.auth.get_token(owner_open_id, rejected_token=token)[
                    'access_token']
                return self._request(path, query, token)
        except AuthorizationRequired:
            # credentials vanished between the API precheck and here; the
            # client retries and gets a fresh authorization link next round.
            raise CalendarError('auth_unavailable') from None
        except AuthUnavailable:
            raise CalendarError('auth_unavailable') from None

    # -- validation -----------------------------------------------------------

    def validate(self, owner_open_id, meeting_ref):
        now = int(self._clock())
        identities = self._identities(owner_open_id)
        events = self._window_events(now, owner_open_id)
        match, organizer_ok, in_window = None, False, False
        for event in events:
            if _meeting_no(event) != (meeting_ref or {}).get('no'):
                continue
            match = event
            organizer_ok = _organizer_id(event) in identities
            start = _timestamp(event, 'start_time')
            end = _timestamp(event, 'end_time')
            in_window = start is not None and end is not None and (
                start - WINDOW_SECONDS <= now <= end + WINDOW_SECONDS)
            break
        if match is None:
            raise CalendarError('meeting_not_found')
        if not organizer_ok:
            raise CalendarError('not_organizer')
        if not in_window:
            raise CalendarError('outside_window')
        calendar_id = match['_calendar_id']
        raw_id = match['event_id']
        event_id = raw_id if '/' in raw_id else f'{calendar_id}/{raw_id}'
        return {'event_calendar_id': calendar_id,
                'event_id': event_id,
                'title': (match.get('summary') or '')[:120],
                'description': (match.get('description') or '')[:20000]}

    def _identities(self, owner_open_id):
        data = self._get(USER_INFO_PATH, None, owner_open_id).get('data')
        identities = []
        if isinstance(data, dict):
            for key in ('user_id', 'open_id', 'union_id'):
                value = data.get(key)
                if isinstance(value, str) and ID_TEXT.fullmatch(value):
                    identities.append(value)
        if not identities:
            raise CalendarError('auth_unavailable')
        return frozenset(identities)

    def list_meetings(self, owner_open_id, *, days=7, limit=10):
        """Upcoming meetings (plan-C T1): recent + next ``days`` days, with the
        nine-digit meeting number extracted from vchat.meeting_url when present."""
        now = int(self._clock())
        identities = self._identities(owner_open_id)
        events = self._window_events(now, owner_open_id)
        meetings = []
        for event in events:
            start = _timestamp(event, 'start_time')
            end = _timestamp(event, 'end_time')
            if start is None or end is None or end < start:
                continue
            if end < now - WINDOW_SECONDS or start > now + days * 86400:
                continue
            meetings.append({
                'meeting_no': _meeting_no(event),
                'title': (event.get('summary') or '(无标题)')[:120],
                'start_ts': start, 'end_ts': end,
                'is_organizer': _organizer_id(event) in identities,
                'in_progress': start <= now <= end,
            })
        meetings.sort(key=lambda item: item['start_ts'])
        return {'now': now, 'meetings': meetings[:limit]}

    def _window_events(self, now, owner_open_id):
        data = self._get(CALENDARS_PATH, {'page_size': 50}, owner_open_id).get('data')
        items = data.get('calendar_list') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CalendarError('auth_unavailable')
        calendar_id = None
        for item in items:
            if isinstance(item, dict) and item.get('type') == 'primary':
                candidate = item.get('calendar_id')
                if isinstance(candidate, str) and ID_TEXT.fullmatch(candidate):
                    calendar_id = candidate
                    break
        if calendar_id is None:
            raise CalendarError('meeting_not_found')
        path = EVENTS_PATH.format(calendar_id=parse.quote(calendar_id, safe=''))
        data = self._get(path, {'start_time': now - LIST_RANGE_SECONDS,
                                'end_time': now + LIST_RANGE_SECONDS,
                                'page_size': 250}, owner_open_id).get('data')
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CalendarError('auth_unavailable')
        events = []
        for item in items:
            if not isinstance(item, dict) or item.get('status') == 'cancelled':
                continue
            if _timestamp(item, 'start_time') is None:
                continue  # all-day/malformed
            item['_calendar_id'] = calendar_id
            events.append(item)
        return events


def _meeting_no(event: dict) -> str | None:
    vchat = event.get('vchat')
    url = vchat.get('meeting_url') if isinstance(vchat, dict) else None
    if not isinstance(url, str):
        return None
    match = MEETING_NO_IN_URL.search(url)
    return match.group(1) if match else None


class _TokenRejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(str(code))
