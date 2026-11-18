"""Real calendar validation (plan-1 asset map: feishu_calendar.py ported to
the advisor; meeting_no <-> event matching via vchat.meeting_url).

Checks per REQUIREMENTS S1: the meeting exists, the requesting user is the
event organizer, and now is within [start-30min, end+30min]. The event
description becomes the session material (plan-C decision).

Recurring events (2026-11-18 bug, 小A daily meeting): the events list API
never returns an expanded "today's occurrence"; it returns the series master
(``<uid>_0``, ``recurrence`` RRULE set, start/end = FIRST occurrence) plus
exception events for edited instances (``<uid>_<original_time>``,
``is_exception``/``recurring_event_id``), regardless of the query window
(list API anchor_time note: "all recurring events will be retrieved including
those with past occurrences"). Masters must therefore be expanded locally and
same-series entries merged, or daily meetings vanish from 会议列表 (master's
first-occurrence timestamps look long past) and collide on one meeting_no
(meeting_ambiguous). Instance binding follows the legacy 方案 B rule
(legacy/advisor/src/feishu_meeting_adviser/materials.py): recurring series
bind to ``<uid>_<occurrence_start>``, never to the ``_0`` master.
"""
import json
import logging
import re
import time
from datetime import date as _date, datetime as _datetime
from datetime import time as _dtime, timedelta as _timedelta
from datetime import timezone as _timezone
from urllib import error, parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .api import CalendarError
from .auth_client import AuthUnavailable, AuthorizationRequired, HttpAuthClient

LOG = logging.getLogger(__name__)

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

DEFAULT_TZ_NAME = 'Asia/Shanghai'  # Feishu default for non-all-day events
WEEKDAY_CODES = {'MO': 0, 'TU': 1, 'WE': 2, 'TH': 3, 'FR': 4, 'SA': 5, 'SU': 6}
RRULE_ITERATION_CAP = 100_000  # defensive bound for long-running series


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


def _attendee_ids(event: dict) -> set[str]:
    """All user identifiers found in attendees[] (open_id/user_id/union_id mix).

    Used for the 小C invite-list admission path (STATE_MACHINES v2 §0); the
    real API shape is verified in SM-04 against live data.
    """
    ids: set[str] = set()
    attendees = event.get('attendees')
    if not isinstance(attendees, list):
        return ids
    for attendee in attendees:
        if not isinstance(attendee, dict):
            continue
        for key in ('open_id', 'user_id', 'union_id'):
            candidate = attendee.get(key)
            if isinstance(candidate, str) and ID_TEXT.fullmatch(candidate):
                ids.add(candidate)
    return ids


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
        # macOS/VPN flake (2026-11-12 实测): connection setup intermittently
        # fails with Errno 22 before any byte is sent; a fresh socket usually
        # succeeds. GETs are retry-safe by nature.
        attempts, delay = 3, 0.3
        status, raw = 0, b''
        for attempt in range(1, attempts + 1):
            try:
                try:
                    response = self._open(req, timeout=self.timeout)
                except error.HTTPError as exc:
                    response = exc
                with response:
                    status = getattr(response, 'status',
                                     getattr(response, 'code', 200))
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                break
            except (error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, 'reason', exc)
                if attempt == attempts:
                    LOG.warning('calendar transport failed after %d attempts: '
                                'path=%s reason=%r', attempts, path, reason)
                    raise CalendarError('auth_unavailable') from None
                LOG.warning('calendar transport retry %d/%d: path=%s reason=%r',
                            attempt, attempts, path, reason)
                time.sleep(delay)
                delay *= 2
        try:
            result = json.loads(raw[:MAX_RESPONSE_BYTES])
        except (ValueError, UnicodeDecodeError):
            LOG.warning('calendar reply not json: path=%s status=%s len=%d',
                        path, status, len(raw))
            raise CalendarError('auth_unavailable') from None
        code = result.get('code')
        if 200 <= status < 300 and code == 0:
            return result
        if isinstance(code, int) and code in TOKEN_INVALID_CODES:
            raise _TokenRejected(code)
        # Real provider rejection (e.g. transient 99992402 right after a
        # fresh OAuth grant): surface the code/log_id for diagnosis instead
        # of swallowing it into a generic "unavailable".
        LOG.warning('calendar api rejected: path=%s status=%s code=%s msg=%s log_id=%s',
                    path, status, code, result.get('msg'),
                    (result.get('error') or {}).get('log_id')
                    if isinstance(result.get('error'), dict) else None)
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
        """Resolve the authoritative occurrence for meeting_ref (STATE_MACHINES v2 §0).

        Recurring series are expanded locally and merged with their exception
        events (list API returns masters, not occurrences): a daily meeting
        binds to today's occurrence instance (``<uid>_<start>``), never to the
        ``_0`` master; split series ("此日程及后续" edits share one meeting_no)
        resolve to whichever occurrence covers now. Returns binding +
        occurrence data (organizer id, attendee ids, start/end). Raises
        CalendarError(meeting_not_found | meeting_ambiguous | not_organizer |
        outside_window | auth_unavailable).
        """
        now = int(self._clock())
        identities = self._identities(owner_open_id)
        events = self._window_events(now, owner_open_id)
        meeting_no = (meeting_ref or {}).get('no')
        matches = [event for event in events
                   if _meeting_no(event) == meeting_no]
        if not matches:
            raise CalendarError('meeting_not_found')
        candidates = _candidate_occurrences(events, meeting_no,
                                            now - LIST_RANGE_SECONDS - 86400,
                                            now + LIST_RANGE_SECONDS)
        in_window = [row for row in candidates
                     if row[1] - WINDOW_SECONDS <= now <= row[2] + WINDOW_SECONDS]
        if len(in_window) > 1:
            raise CalendarError('meeting_ambiguous')
        if not in_window:
            # candidates exist but none covers now; an empty candidate set
            # means every occurrence was deleted (cancelled exceptions)
            raise CalendarError('outside_window' if candidates
                                else 'meeting_not_found')
        match, start, end = in_window[0]
        organizer_ok = _organizer_id(match) in identities
        calendar_id = match['_calendar_id']
        raw_id = match['event_id']
        if _is_master(match) and not _is_exception(match):
            raw_id = f'{_series_uid(raw_id)}_{int(start)}'
        event_id = raw_id if '/' in raw_id else f'{calendar_id}/{raw_id}'
        # organizer_ok is returned, not raised: the admission decision
        # (A organizer path vs C grant+invite path) belongs to the caller
        # (STATE_MACHINES v2 §0 / CONTRACT_ADVISOR_API v2.0 §2).
        return {'event_calendar_id': calendar_id,
                'event_id': event_id,
                'title': (match.get('summary') or '')[:120],
                'description': (match.get('description') or '')[:20000],
                'organizer': _organizer_id(match),
                'organizer_ok': organizer_ok,
                'attendees': _attendee_ids(match),
                'caller_ids': set(identities),
                'start': start,
                'end': end}

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
        nine-digit meeting number extracted from vchat.meeting_url when present.
        Recurring series expand to occurrences around now (masters carry
        first-occurrence timestamps; exceptions override their slot; cancelled
        exceptions drop it)."""
        now = int(self._clock())
        identities = self._identities(owner_open_id)
        events = self._window_events(now, owner_open_id)
        horizon = now + days * 86400
        floor = now - WINDOW_SECONDS

        def row(event, start, end):
            return {'meeting_no': _meeting_no(event),
                    'title': (event.get('summary') or '(无标题)')[:120],
                    'start_ts': start, 'end_ts': end,
                    'is_organizer': _organizer_id(event) in identities,
                    'in_progress': start <= now <= end}

        numbers, unnumbered = [], []
        for event in events:
            number = _meeting_no(event)
            if number is None:
                unnumbered.append(event)
            elif number not in numbers:
                numbers.append(number)
        meetings = []
        for number in numbers:
            for event, start, end in _candidate_occurrences(
                    events, number, floor - 86400, horizon):
                if end < floor or start > horizon:
                    continue
                meetings.append(row(event, start, end))
        for event in unnumbered:  # no vchat link: plain rows, pre-fix behavior
            if event.get('status') == 'cancelled':
                continue
            start, end = _timestamp(event, 'start_time'), _timestamp(event, 'end_time')
            if start is None or end is None or end < start:
                continue
            if end < floor or start > horizon:
                continue
            meetings.append(row(event, start, end))
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
            if not isinstance(item, dict) or _timestamp(item, 'start_time') is None:
                continue  # all-day/malformed
            # cancelled items are kept: cancelled exceptions mark deleted
            # occurrences and must suppress their series slot (consumers
            # filter cancelled entries themselves)
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


# -- recurring-event semantics -------------------------------------------------

def _is_master(event: dict) -> bool:
    """Series master: carries the RRULE; start/end = FIRST occurrence."""
    rule = event.get('recurrence')
    return isinstance(rule, str) and bool(rule.strip())


def _is_exception(event: dict) -> bool:
    """Edited/deleted instance of a recurring series (docs: recurrence empty)."""
    if event.get('is_exception') is True:
        return True
    parent = event.get('recurring_event_id')
    return isinstance(parent, str) and bool(parent.strip())


def _series_uid(event_id: str) -> str:
    """``<uid>_0`` / ``<uid>_<ts>`` -> ``<uid>``; plain ids pass through."""
    base, sep, suffix = event_id.rpartition('_')
    if sep and suffix.isdigit():
        return base
    return event_id


def _instance_marker(event_id: str) -> int | None:
    """Trailing ``_<n>`` of an exception id = original occurrence start."""
    base, sep, suffix = event_id.rpartition('_')
    if sep and suffix.isdigit() and suffix != '0':
        return int(suffix)
    return None


def _event_tz(event: dict):
    start = event.get('start_time')
    name = start.get('timezone') if isinstance(start, dict) else None
    try:
        return ZoneInfo(name if isinstance(name, str) and name else DEFAULT_TZ_NAME)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return _timezone(_timedelta(hours=8))


def _parse_until(raw: str | None) -> int | None:
    """UNTIL bound (inclusive). Date-only form covers the whole local day."""
    if raw is None:
        return None
    match = re.fullmatch(r'(\d{8})(?:T(\d{6})Z?)?', raw)
    if match is None:
        raise ValueError(raw)
    day = _datetime.strptime(match.group(1), '%Y%m%d').date()
    if match.group(2):
        digits = match.group(2)
        moment = _dtime(int(digits[:2]), int(digits[2:4]), int(digits[4:6]))
        return int(_datetime.combine(day, moment, tzinfo=_timezone.utc).timestamp())
    next_day = _datetime.combine(day + _timedelta(days=1), _dtime(0),
                                 tzinfo=_timezone(_timedelta(hours=8))).timestamp()
    return int(next_day) - 1


def _parse_weekdays(raw: str) -> list[int]:
    days = []
    for token in raw.split(','):
        code = token[-2:]
        if len(token) > 2 or code not in WEEKDAY_CODES:
            raise ValueError(raw)
        days.append(WEEKDAY_CODES[code])
    return sorted(set(days))


def _parse_monthdays(raw: str) -> list[int]:
    values = [int(token) for token in raw.split(',')]
    if any(value == 0 or abs(value) > 31 for value in values):
        raise ValueError(raw)
    return values


def _parse_rrule(rule: str) -> dict | None:
    """Minimal RFC5545 subset sufficient for Feishu UI recurrences:
    FREQ=DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY (weekly),
    BYMONTHDAY (monthly). Returns None when the rule is unsupported -> the
    caller falls back to treating the master as a single first-occurrence
    event (pre-fix behavior)."""
    try:
        parts = {}
        for chunk in rule.split(';'):
            key, sep, value = chunk.partition('=')
            if not sep or not key.strip():
                return None
            parts[key.strip().upper()] = value.strip().upper()
        freq = parts.get('FREQ')
        allowed = {'FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'BYDAY', 'BYMONTHDAY', 'WKST'}
        if freq not in ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY') or set(parts) - allowed:
            return None
        if 'WKST' in parts and parts['WKST'] != 'MO':
            return None
        interval = int(parts['INTERVAL']) if 'INTERVAL' in parts else 1
        count = int(parts['COUNT']) if 'COUNT' in parts else None
        if interval < 1 or (count is not None and count < 1):
            return None
        until = _parse_until(parts.get('UNTIL'))
        byday = bymonthday = None
        if freq == 'WEEKLY':
            if 'BYMONTHDAY' in parts:
                return None
            if 'BYDAY' in parts:
                byday = _parse_weekdays(parts['BYDAY'])
        elif freq == 'MONTHLY':
            if 'BYDAY' in parts:  # nth-weekday (e.g. 2TU) unsupported
                return None
            if 'BYMONTHDAY' in parts:
                bymonthday = _parse_monthdays(parts['BYMONTHDAY'])
        elif 'BYDAY' in parts or 'BYMONTHDAY' in parts:
            return None
        return {'freq': freq, 'interval': interval, 'count': count,
                'until': until, 'byday': byday, 'bymonthday': bymonthday}
    except (ValueError, OverflowError):
        return None


def _resolve_monthday(year: int, month: int, day_number: int):
    """Calendar date for a (possibly negative) BYMONTHDAY; None when invalid."""
    length = _month_length(year, month)
    if day_number < 0:
        day_number += length + 1
    if 1 <= day_number <= length:
        return _date(year, month, day_number)
    return None


def _month_length(year: int, month: int) -> int:
    if month == 12:
        nxt = _date(year + 1, 1, 1)
    else:
        nxt = _date(year, month + 1, 1)
    return (nxt - _timedelta(days=1)).day


def _occurrence_starts(master: dict, rule: dict, lo: int, hi: int):
    """Yield occurrence start timestamps of a series within [lo, hi].

    Chronological from the master's first occurrence so COUNT/UNTIL apply
    correctly even when lo is far ahead of the series start. Absolute-second
    daily stepping and same-local-time weekly/monthly/yearly stepping follow
    the event timezone (no DST in the default Asia/Shanghai)."""
    start0 = _timestamp(master, 'start_time')
    if start0 is None:
        return
    count, until, interval = rule['count'], rule['until'], rule['interval']
    produced = 0
    if rule['freq'] == 'DAILY':
        cursor = start0
        step = interval * 86400
        while cursor <= hi:
            if count is not None and produced >= count:
                return
            if until is not None and cursor > until:
                return
            if cursor >= lo:
                yield cursor
            produced += 1
            if produced > RRULE_ITERATION_CAP:
                return
            cursor += step
        return
    tz = _event_tz(master)
    local0 = _datetime.fromtimestamp(start0, tz)
    moment = local0.timetz()
    if rule['freq'] == 'WEEKLY':
        days = rule['byday'] or [local0.weekday()]
        week0 = local0.date() - _timedelta(days=local0.weekday())
        if until is not None and start0 > until:
            return
        if local0.weekday() not in days:  # RFC5545: DTSTART is occurrence #1
            produced = 1
            if start0 >= lo:
                yield start0
        week = 0
        while True:
            base_date = week0 + _timedelta(days=7 * interval * week)
            if _datetime.combine(base_date, _dtime(0), tzinfo=tz).timestamp() > hi:
                return
            for weekday in days:
                moment_dt = _datetime.combine(base_date + _timedelta(days=weekday),
                                              moment)
                ts = int(moment_dt.timestamp())
                if ts < start0 or ts > hi:
                    continue
                if count is not None and produced >= count:
                    return
                if until is not None and ts > until:
                    return
                if ts >= lo:
                    yield ts
                produced += 1
                if produced > RRULE_ITERATION_CAP:
                    return
            week += 1
    elif rule['freq'] == 'MONTHLY':
        day_numbers = rule['bymonthday'] or [local0.day]
        months = local0.year * 12 + local0.month - 1
        step = 0
        while True:
            year, month = divmod(months + interval * step, 12)
            month += 1
            if _datetime.combine(_date(year, month, 1), _dtime(0),
                                 tzinfo=tz).timestamp() > hi:
                return
            for day_number in day_numbers:
                day = _resolve_monthday(year, month, day_number)
                if day is None:
                    continue
                ts = int(_datetime.combine(day, moment).timestamp())
                if ts < start0 or ts > hi:
                    continue
                if count is not None and produced >= count:
                    return
                if until is not None and ts > until:
                    return
                if ts >= lo:
                    yield ts
                produced += 1
                if produced > RRULE_ITERATION_CAP:
                    return
            step += 1
    else:  # YEARLY
        step = 0
        while True:
            try:
                day = _date(local0.year + interval * step, local0.month, local0.day)
            except ValueError:  # Feb 29 in a non-leap year
                day = None
            if day is not None:
                ts = int(_datetime.combine(day, moment).timestamp())
                if ts > hi:
                    return
                if ts >= start0:
                    if count is not None and produced >= count:
                        return
                    if until is not None and ts > until:
                        return
                    if ts >= lo:
                        yield ts
                    produced += 1
                    if produced > RRULE_ITERATION_CAP:
                        return
            step += 1


def _series_overrides(events, meeting_no):
    """Exception events grouped for one meeting_no: overrides (edited
    instances keyed by original occurrence start) and suppressed times
    (cancelled instances = deleted occurrences)."""
    overrides: dict[str, dict[int, dict]] = {}
    suppressed: dict[str, set[int]] = {}
    for event in events:
        if _meeting_no(event) != meeting_no or not _is_exception(event):
            continue
        uid = _series_uid(event.get('recurring_event_id') or event.get('event_id')
                          or '')
        marker = _instance_marker(event.get('event_id') or '')
        if marker is None:
            marker = _timestamp(event, 'start_time')
        if not uid or marker is None:
            continue
        if event.get('status') == 'cancelled':
            suppressed.setdefault(uid, set()).add(marker)
        else:
            overrides.setdefault(uid, {})[marker] = event
    return overrides, suppressed


def _candidate_occurrences(events, meeting_no, lo, hi):
    """(event, start, end) occurrence candidates for meeting_no within
    [lo, hi]: direct events at their own times plus locally expanded series
    masters (skipping exception-overridden or deleted slots). Masters with an
    unsupported RRULE fall back to their first occurrence (pre-fix behavior).
    Same-series master+exception entries therefore no longer collide."""
    overrides, suppressed = _series_overrides(events, meeting_no)
    candidates = []
    masters = []
    for event in events:
        if _meeting_no(event) != meeting_no or event.get('status') == 'cancelled':
            continue
        if _is_exception(event):
            start, end = _timestamp(event, 'start_time'), _timestamp(event, 'end_time')
            if start is not None and end is not None and end >= start:
                candidates.append((event, start, end))
        elif _is_master(event):
            masters.append(event)
        else:
            start, end = _timestamp(event, 'start_time'), _timestamp(event, 'end_time')
            if start is not None and end is not None and end >= start:
                candidates.append((event, start, end))
    for master in masters:
        uid = _series_uid(master.get('event_id') or '')
        start0, end0 = _timestamp(master, 'start_time'), _timestamp(master, 'end_time')
        if start0 is None or end0 is None or end0 < start0:
            continue
        rule = _parse_rrule(master.get('recurrence') or '')
        if rule is None:
            candidates.append((master, start0, end0))
            continue
        duration = end0 - start0
        for start in _occurrence_starts(master, rule, lo, hi):
            if start in suppressed.get(uid, ()):
                continue  # this instance was deleted
            if start in overrides.get(uid, {}):
                continue  # the exception event owns this slot at its own time
            candidates.append((master, start, start + duration))
    return candidates


class _TokenRejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(str(code))
