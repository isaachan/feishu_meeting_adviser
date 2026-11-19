"""Probe the calendar chat-group attendee path for the 小C admission bug.

Bug under study (2026-11-19): a supervisor whose only invite proof is
membership in a chat group invited to the calendar event is rejected with
`not_invited` — advisor/src/advisor/calendar.py `_occurrence_attendees`
skips non-user attendee types, so advisor/src/advisor/api.py finds no
`attendees & caller_ids` hit.

Flow, all as the probing user (= 小C; PKCE user OAuth, token memory-only):
  user_info -> primary calendar -> events window (±12h, meeting_no match)
  -> occurrence resolution -> attendees list (raw dump; chat attendee shape)
  -> per chat-type attendee:
       GET /calendars/{cid}/events/{eid}/attendees/{attendee_id}/chat_members
  -> verdict: what the advisor answers today vs with group expansion.

Stdlib only. Credentials: BOT_APP_ID/BOT_APP_SECRET (PKCE login) or a
ready FEISHU_USER_ACCESS_TOKEN. See README.md for setup and interpretation.
"""

import argparse
import base64
from datetime import date, datetime, time as dtime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import secrets
import sys
import time
from urllib import error, parse, request
from zoneinfo import ZoneInfo

API = 'https://open.feishu.cn/open-apis'
AUTHORIZE = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
TOKEN = API + '/authen/v2/oauth/token'
SCOPES = 'calendar:calendar:read calendar:calendar.event:read'
LIST_RANGE_SECONDS = 12 * 3600
WINDOW_SECONDS = 30 * 60
PAGE_CAP = 20
ID_TEXT = re.compile(r'[A-Za-z0-9_.@~-]{1,256}\Z')
MEETING_NO_IN_URL = re.compile(r'/j/([0-9]{9})')
TZ = ZoneInfo('Asia/Shanghai')
WEEKDAY_CODES = {'MO': 0, 'TU': 1, 'WE': 2, 'TH': 3, 'FR': 4, 'SA': 5, 'SU': 6}


def emit(phase, **payload):
    print(json.dumps({'phase': phase, **payload}, ensure_ascii=False), flush=True)


# -- HTTP -------------------------------------------------------------------

def diagnostic(operation, http_status, result):
    code = result.get('code') if isinstance(result, dict) else None
    msg = result.get('msg') if isinstance(result, dict) else None
    detail = f'飞书 {operation}：HTTP {http_status}，API code={code}'
    if isinstance(msg, str) and msg:
        detail += f'，msg={msg[:200]!r}'
    return RuntimeError(detail)


def fetch(url, *, operation, token):
    req = request.Request(url, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        try:
            result = json.load(exc)
        except (ValueError, OSError):
            result = None
        raise diagnostic(operation, exc.code, result) from None
    except (error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f'飞书 {operation} 请求失败：{type(exc).__name__}') from None
    if not isinstance(result, dict) or result.get('code') != 0:
        raise diagnostic(operation, 200, result)
    return result.get('data') or {}


def fetch_pages(path, token, *, operation, page_cap=PAGE_CAP):
    """Paged GET -> (items, pages, complete). Mirrors advisor paging style."""
    items, page_token, pages = [], '', 0
    while pages < page_cap:
        query = {'user_id_type': 'open_id', 'page_size': 100}
        if page_token:
            query['page_token'] = page_token
        data = fetch(f'{API}{path}?{parse.urlencode(query)}',
                     operation=operation, token=token)
        chunk = data.get('items')
        items.extend(chunk if isinstance(chunk, list) else [])
        pages += 1
        if not data.get('has_more'):
            return items, pages, True
        page_token = data.get('page_token')
        if not isinstance(page_token, str) or not page_token:
            return items, pages, True
    return items, pages, False


# -- user OAuth (PKCE, memory-only token) ------------------------------------

def parse_token_response(result):
    token = result.get('access_token') if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        keys = sorted(result) if isinstance(result, dict) else 'not a dict'
        raise RuntimeError(f'换 token 失败：响应无 access_token（keys: {keys}）')
    granted = set((result.get('scope') or '').split())
    missing = [s for s in SCOPES.split() if s not in granted]
    if missing:
        print(f'警告：授权 scope 缺失 {missing}（实际：{sorted(granted) or "<空>"}）。'
              '需在应用开通对应用户身份权限并发布后重新授权。', file=sys.stderr)
    return token


def exchange(code, verifier, redirect, app_id, app_secret):
    payload = json.dumps({
        'grant_type': 'authorization_code', 'client_id': app_id,
        'client_secret': app_secret, 'code': code, 'redirect_uri': redirect,
        'code_verifier': verifier, 'scope': SCOPES,
    }).encode()
    req = request.Request(TOKEN, data=payload, headers={
        'Content-Type': 'application/json; charset=utf-8',
        'Accept': 'application/json'})
    try:
        with request.urlopen(req, timeout=15) as response:
            return parse_token_response(json.load(response))
    except error.HTTPError as exc:
        try:
            body = json.loads(exc.read(65537))
            reason = body.get('error_description') or body.get('error') or ''
        except (ValueError, OSError):
            reason = ''
        raise RuntimeError(
            f'换 token 失败：HTTP {exc.code}{("，" + str(reason)[:200]) if reason else ""}'
        ) from None
    except (error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f'换 token 请求失败：{type(exc).__name__}') from None


def authorize(app_id, app_secret, port):
    """One-shot local PKCE login; token stays in memory only."""
    redirect = f'http://127.0.0.1:{port}/callback'
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    login_url = AUTHORIZE + '?' + parse.urlencode({
        'client_id': app_id, 'response_type': 'code', 'redirect_uri': redirect,
        'scope': SCOPES, 'state': state, 'code_challenge': challenge,
        'code_challenge_method': 'S256', 'prompt': 'consent',
    })
    outcome = {'code': None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            valid = False
            if self.headers.get('Host') == f'127.0.0.1:{port}' and len(self.path) < 8192:
                parts = parse.urlsplit(self.path)
                if parts.path == '/callback':
                    try:
                        fields = parse.parse_qs(parts.query, max_num_fields=10)
                        codes = fields.get('code', [])
                        states = fields.get('state', [])
                        valid = (len(codes) == len(states) == 1 and
                                 secrets.compare_digest(states[0], state)
                                 and bool(codes[0])
                                 and not fields.get('error')
                                 and outcome['code'] is None)
                    except ValueError:
                        pass
            if valid:
                outcome['code'] = codes[0]
            body = (b'Authorization received; return to the terminal.' if valid
                    else b'Invalid callback.')
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(('127.0.0.1', port), Handler) as server:
        server.timeout = 1
        print(f'请在浏览器打开以下链接，用小C本人的飞书账号授权（10 分钟内有效）：\n'
              f'{login_url}', file=sys.stderr, flush=True)
        deadline = time.monotonic() + 600
        while outcome['code'] is None and time.monotonic() < deadline:
            server.handle_request()
    if outcome['code'] is None:
        raise RuntimeError('用户授权超时')
    return exchange(outcome['code'], verifier, redirect, app_id, app_secret)


# -- pure helpers (offline-tested) -------------------------------------------

def meeting_no(event):
    vchat = event.get('vchat')
    url = vchat.get('meeting_url') if isinstance(vchat, dict) else None
    if not isinstance(url, str):
        return None
    match = MEETING_NO_IN_URL.search(url)
    return match.group(1) if match else None


def item_ids(item):
    """User ids carried by an attendee / chat-member / organizer dict."""
    ids = set()
    if isinstance(item, dict):
        for key in ('user_id', 'open_id', 'union_id'):
            value = item.get(key)
            if isinstance(value, str) and ID_TEXT.fullmatch(value):
                ids.add(value)
    return ids


def any_id_hit(rows, caller_ids):
    return any(item_ids(row) & set(caller_ids or ())
               for row in rows or [] if isinstance(row, dict))


def chat_attendee_ids(items):
    """attendee_ids of chat-type attendees (type=chat or chat_ prefix)."""
    ids = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        aid = item.get('attendee_id')
        is_chat = item.get('type') == 'chat' or (
            isinstance(aid, str) and aid.startswith('chat_'))
        if is_chat and isinstance(aid, str) and aid:
            ids.append(aid)
    return ids


def organizer_ids(event):
    ids = set()
    for field in ('event_organizer', 'organizer', 'creator'):
        value = event.get(field)
        if isinstance(value, dict):
            ids |= item_ids(value)
        elif isinstance(value, str) and ID_TEXT.fullmatch(value):
            ids.add(value)
    return ids


def _ts(event, key):
    value = event.get(key)
    if not isinstance(value, dict):
        return None
    ts = value.get('timestamp')
    if isinstance(ts, str) and ts.isdigit() and len(ts) <= 12:
        return int(ts)
    return ts if isinstance(ts, int) and not isinstance(ts, bool) else None


def _series_uid(event_id):
    base, sep, suffix = (event_id or '').rpartition('_')
    return base if sep and suffix.isdigit() else event_id


def _parse_rrule(rule):
    """Minimal subset of advisor calendar.py: FREQ/INTERVAL/COUNT/UNTIL,
    BYDAY (weekly), BYMONTHDAY (monthly). None -> unsupported rule."""
    try:
        parts = {}
        for chunk in rule.split(';'):
            key, sep, value = chunk.partition('=')
            if not sep or not key.strip():
                return None
            parts[key.strip().upper()] = value.strip().upper()
        freq = parts.get('FREQ')
        allowed = {'FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'BYDAY', 'BYMONTHDAY', 'WKST'}
        if freq not in ('DAILY', 'WEEKLY', 'MONTHLY') or set(parts) - allowed:
            return None
        if 'WKST' in parts and parts['WKST'] != 'MO':
            return None
        interval = int(parts['INTERVAL']) if 'INTERVAL' in parts else 1
        count = int(parts['COUNT']) if 'COUNT' in parts else None
        if interval < 1 or (count is not None and count < 1):
            return None
        until = None
        if 'UNTIL' in parts:
            match = re.fullmatch(r'(\d{8})(?:T(\d{6})Z?)?', parts['UNTIL'])
            if match is None:
                return None
            day = datetime.strptime(match.group(1), '%Y%m%d').date()
            if match.group(2):
                digits = match.group(2)
                until = int(datetime.combine(
                    day, dtime(int(digits[:2]), int(digits[2:4]), int(digits[4:6])),
                    tzinfo=timezone.utc).timestamp())
            else:
                until = int(datetime.combine(
                    day + timedelta(days=1), dtime(0),
                    tzinfo=timezone(timedelta(hours=8))).timestamp()) - 1
        byday = bymonthday = None
        if 'BYDAY' in parts:
            if freq != 'WEEKLY':
                return None
            try:
                byday = sorted({WEEKDAY_CODES[t] for t in parts['BYDAY'].split(',')})
            except KeyError:
                return None
        if 'BYMONTHDAY' in parts:
            if freq != 'MONTHLY':
                return None
            try:
                bymonthday = [int(t) for t in parts['BYMONTHDAY'].split(',')]
            except ValueError:
                return None
            if any(d == 0 or abs(d) > 31 for d in bymonthday):
                return None
        return {'freq': freq, 'interval': interval, 'count': count,
                'until': until, 'byday': byday, 'bymonthday': bymonthday}
    except (ValueError, OverflowError):
        return None


def _occurrence_starts(start0, rule, lo, hi):
    """Occurrence start timestamps within [lo, hi], chronological (compact
    port of advisor _occurrence_starts; fixed Asia/Shanghai local time)."""
    count, until, interval = rule['count'], rule['until'], rule['interval']
    produced = 0
    if rule['freq'] == 'DAILY':
        cursor = start0
        while cursor <= hi:
            if count is not None and produced >= count:
                return
            if until is not None and cursor > until:
                return
            if cursor >= lo:
                yield cursor
            produced += 1
            cursor += interval * 86400
        return
    local0 = datetime.fromtimestamp(start0, TZ)
    moment = local0.timetz()
    if rule['freq'] == 'WEEKLY':
        days = rule['byday'] or [local0.weekday()]
        week0 = local0.date() - timedelta(days=local0.weekday())
        if local0.weekday() not in days:  # DTSTART is occurrence #1
            produced = 1
            if start0 >= lo:
                yield start0
        week = 0
        while True:
            base = week0 + timedelta(days=7 * interval * week)
            if datetime.combine(base, dtime(0), tzinfo=TZ).timestamp() > hi:
                return
            for weekday in days:
                ts = int(datetime.combine(base + timedelta(days=weekday),
                                          moment).timestamp())
                if ts < start0 or ts > hi:
                    continue
                if count is not None and produced >= count:
                    return
                if until is not None and ts > until:
                    return
                if ts >= lo:
                    yield ts
                produced += 1
            week += 1
    else:  # MONTHLY
        day_numbers = rule['bymonthday'] or [local0.day]
        months = local0.year * 12 + local0.month - 1
        step = 0
        while True:
            year, month = divmod(months + interval * step, 12)
            month += 1
            if datetime.combine(date(year, month, 1), dtime(0),
                                tzinfo=TZ).timestamp() > hi:
                return
            nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            last_day = (nxt - timedelta(days=1)).day
            for number in day_numbers:
                day_number = number + last_day + 1 if number < 0 else number
                if not 1 <= day_number <= last_day:
                    continue
                ts = int(datetime.combine(date(year, month, day_number),
                                          moment).timestamp())
                if ts < start0 or ts > hi:
                    continue
                if count is not None and produced >= count:
                    return
                if until is not None and ts > until:
                    return
                if ts >= lo:
                    yield ts
                produced += 1
            step += 1


def resolve_instance(events, number, now):
    """(event, event_id, start, end) occurrence candidates covering now.

    Compact probe port of advisor calendar.py (no exception-override
    merging): direct events bind at their own id; recurring masters bind
    to '<uid>_<start>'; unsupported RRULE falls back to the master id.
    """
    candidates = []
    for event in events:
        if meeting_no(event) != number or event.get('status') == 'cancelled':
            continue
        start, end = _ts(event, 'start_time'), _ts(event, 'end_time')
        if start is None or end is None or end < start:
            continue
        if (event.get('recurrence') or '').strip():
            rule = _parse_rrule(event.get('recurrence') or '')
            if rule is None:
                candidates.append((event, event.get('event_id'), start, end))
                continue
            for occurrence in _occurrence_starts(start, rule, now - 18 * 3600,
                                                  now + 18 * 3600):
                candidates.append(
                    (event, f'{_series_uid(event.get("event_id"))}_{occurrence}',
                     occurrence, occurrence + (end - start)))
        else:
            candidates.append((event, event.get('event_id'), start, end))
    return [row for row in candidates
            if row[2] - WINDOW_SECONDS <= now <= row[3] + WINDOW_SECONDS]


# -- API walkers --------------------------------------------------------------

def primary_calendar(token):
    data = fetch(API + '/calendar/v4/calendars?page_size=50',
                 operation='获取日历列表', token=token)
    for item in data.get('calendar_list') or []:
        if isinstance(item, dict) and item.get('type') == 'primary':
            calendar_id = item.get('calendar_id')
            if isinstance(calendar_id, str) and calendar_id:
                return calendar_id
    raise RuntimeError('未找到 primary 日历')


def window_events(calendar_id, token, now):
    """Events on the caller's primary calendar within ±12h (advisor shape)."""
    path = (f'/calendar/v4/calendars/{parse.quote(calendar_id, safe="")}/events')
    data = fetch(f'{API}{path}?' + parse.urlencode({
        'start_time': now - LIST_RANGE_SECONDS,
        'end_time': now + LIST_RANGE_SECONDS, 'page_size': 250}),
        operation='获取日程列表', token=token)
    items = data.get('items')
    return [e for e in (items or []) if isinstance(e, dict)]


# -- main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='按会议号核对小C邀请准入：attendees 原始形状 + 群成员展开')
    parser.add_argument('meeting_no', help='9 位会议号（日程 vchat 链接中的号码）')
    parser.add_argument('--calendar-id', help='手动指定日历 id（跳过自动发现）')
    parser.add_argument('--event-id', help='手动钉死日程/实例 id（跳过 occurrence 解析）')
    parser.add_argument('--port', type=int, default=8767,
                        help='本机授权回调端口，默认 8767（须已在应用后台登记）')
    args = parser.parse_args()
    if len(args.meeting_no) != 9 or not args.meeting_no.isdecimal():
        parser.error('meeting_no 须为 9 位数字')

    token = os.getenv('FEISHU_USER_ACCESS_TOKEN')
    if token:
        emit('auth', mode='env_token')
    else:
        app_id, app_secret = os.getenv('BOT_APP_ID'), os.getenv('BOT_APP_SECRET')
        if not app_id or not app_secret:
            parser.error('请设置 BOT_APP_ID/BOT_APP_SECRET 或 FEISHU_USER_ACCESS_TOKEN')
        token = authorize(app_id, app_secret, args.port)
        emit('auth', mode='pkce', scopes=SCOPES.split())

    user = fetch(API + '/authen/v1/user_info',
                 operation='获取用户信息', token=token).get('user') or {}
    caller_ids = {user.get(key) for key in ('user_id', 'open_id', 'union_id')
                  if isinstance(user.get(key), str) and user.get(key)}
    emit('user_info', user=user)

    try:
        calendar_id = args.calendar_id or primary_calendar(token)
    except RuntimeError as exc:
        emit('calendars', error=str(exc))
        return
    emit('calendars', primary_calendar_id=calendar_id, manual=bool(args.calendar_id))

    now = int(time.time())
    try:
        events = window_events(calendar_id, token, now)
    except RuntimeError as exc:
        emit('events', error=str(exc))
        return
    matched = [e for e in events if meeting_no(e) == args.meeting_no]
    emit('events', matched=matched, matched_count=len(matched))
    if not matched:
        emit('verdict', error='meeting_not_found_on_caller_calendar',
             note='群邀日程未出现在授权用户自己的日历中；bug 形态比 not_invited 更早，'
                  '修复思路需另行评估（见 README 判读口径第 1 问）')
        return

    if args.event_id:
        chosen, event_id = matched[0], args.event_id
        emit('occurrence', event_id=event_id, via='manual')
    else:
        rows = resolve_instance(matched, args.meeting_no, now)
        if len(rows) > 1:
            emit('occurrence', ambiguous=[
                {'event_id': row[1], 'start': row[2]} for row in rows])
            rows = rows[:1]
        if rows:
            chosen, event_id, start, _end = rows[0]
            emit('occurrence', event_id=event_id, via='resolved',
                 covering_now=True, start=start)
        else:
            chosen = matched[0]
            event_id = chosen.get('event_id')
            emit('occurrence', event_id=event_id, via='fallback_no_window_cover',
                 covering_now=False,
                 note='advisor 会拒绝 outside_window；探针继续只为转储名单形状')

    attendee_path = (f'/calendar/v4/calendars/{parse.quote(calendar_id, safe="")}'
                     f'/events/{parse.quote(str(event_id), safe="")}/attendees')
    try:
        items, pages, complete = fetch_pages(
            attendee_path, token, operation='获取日程参与人列表')
    except RuntimeError as exc:
        emit('attendees', error=str(exc))
        emit('verdict', error='attendees_fetch_failed', detail=str(exc))
        return
    emit('attendees', items=items, pages=pages, complete=complete)

    live = [i for i in items if isinstance(i, dict)
            and i.get('rsvp_status') != 'removed']
    organizer_ok = bool(caller_ids & organizer_ids(chosen))
    direct_hit = any_id_hit(live, caller_ids)
    chats = chat_attendee_ids(live)
    group_hit, indeterminate = False, False
    for attendee_id in chats:
        member_path = f'{attendee_path}/{parse.quote(attendee_id, safe="")}/chat_members'
        try:
            members, member_pages, member_complete = fetch_pages(
                member_path, token, operation='获取日程参与人群成员列表')
        except RuntimeError as exc:
            emit('chat_members', attendee_id=attendee_id, error=str(exc))
            indeterminate = True
            continue
        emit('chat_members', attendee_id=attendee_id, items=members,
             pages=member_pages, complete=member_complete)
        if any_id_hit(members, caller_ids):
            group_hit = True
    advisor_today = 'admit' if (organizer_ok or direct_hit) else 'not_invited'
    if organizer_ok or direct_hit or group_hit:
        expanded = 'admit'
    elif indeterminate:
        expanded = 'indeterminate'
    else:
        expanded = 'not_invited'
    emit('verdict', organizer_ok=organizer_ok, direct_invite_hit=direct_hit,
         any_chat_attendee=bool(chats), chat_attendee_count=len(chats),
         group_invite_hit=group_hit, had_errors=indeterminate,
         advisor_today=advisor_today, advisor_with_group_expansion=expanded)


if __name__ == '__main__':
    main()
