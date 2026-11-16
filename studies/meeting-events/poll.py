"""Observe ordinary Feishu meetings by polling their state and participants."""

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import time
from urllib import error, parse, request


API = 'https://open.feishu.cn/open-apis'


def diagnostic(operation, http_status, result):
    code = result.get('code') if isinstance(result, dict) else None
    msg = result.get('msg') if isinstance(result, dict) else None
    detail = f'飞书 {operation}：HTTP {http_status}，API code={code}'
    if isinstance(msg, str) and msg:
        detail += f'，msg={msg[:200]!r}'
    return RuntimeError(detail)


def fetch(url, *, operation, token):
    headers = {'Accept': 'application/json'}
    headers['Authorization'] = 'Bearer ' + token
    req = request.Request(url, headers=headers)
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


def get_token(app_id, app_secret):
    req = request.Request(API + '/auth/v3/tenant_access_token/internal',
                          data=json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode(),
                          headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        raise RuntimeError(f'获取应用 token 失败：HTTP {exc.code}') from None
    except (error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f'获取应用 token 失败：{type(exc).__name__}') from None
    if not isinstance(result, dict) or result.get('code') != 0 or not result.get('tenant_access_token'):
        code = result.get('code') if isinstance(result, dict) else None
        raise RuntimeError(f'获取应用 token 失败：code={code}')
    return result['tenant_access_token'], time.monotonic() + int(result.get('expire', 7200)) - 120


def lookup(meeting_no, token):
    now = int(time.time())
    query = {'meeting_no': meeting_no, 'start_time': now - 3600,
             'end_time': now, 'page_size': 50}
    briefs = []
    while True:
        data = fetch(API + '/vc/v1/meetings/list_by_no?' + parse.urlencode(query),
                     operation='按会议号查询', token=token)
        briefs.extend(data.get('meeting_briefs') or [])
        if not data.get('has_more'):
            break
        cursor = data.get('page_token')
        if not cursor or cursor == query.get('page_token'):
            raise RuntimeError('会议列表分页游标无效')
        query['page_token'] = cursor
    return list(dict.fromkeys(str(item['id']) for item in briefs
                              if isinstance(item, dict) and item.get('id')))


def details(meeting_id, token):
    query = parse.urlencode({'with_participants': 'true'})
    data = fetch(API + '/vc/v1/meetings/' + parse.quote(meeting_id, safe='') + '?' + query,
                 operation='获取会议详情', token=token)
    meeting = data.get('meeting')
    if not isinstance(meeting, dict):
        raise RuntimeError('会议详情缺少 meeting')
    return meeting


def observations(before, after):
    """Changes in snapshots, not actual Feishu push events."""
    previous = before or {}
    old_people = {str(p['id']): p for p in previous.get('participants') or [] if p.get('id')}
    new_people = {str(p['id']): p for p in after.get('participants') or [] if p.get('id')}
    if after.get('status') == 2 and previous.get('status') != 2:
        yield '会议开始', None
    for pid, person in new_people.items():
        if person.get('status') == 2 and old_people.get(pid, {}).get('status') != 2:
            yield '人加入', pid
        elif (person.get('status') == 4 and old_people.get(pid, {}).get('status') == 2):
            yield '人退出', pid
    if after.get('status') == 3 and previous.get('status') == 2:
        for pid, person in old_people.items():
            if person.get('status') == 2 and new_people.get(pid, {}).get('status') != 4:
                yield '人退出', pid
        yield '会议结束', None


def main():
    parser = argparse.ArgumentParser(description='按会议号轮询普通飞书会议的开始/结束和人员进出')
    parser.add_argument('meeting_no', help='9 位会议号')
    parser.add_argument('--interval', type=float, default=3, help='轮询间隔秒数，默认 3')
    args = parser.parse_args()
    if len(args.meeting_no) != 9 or not args.meeting_no.isascii() or not args.meeting_no.isdecimal():
        parser.error('meeting_no 须为 9 位数字')
    if args.interval < 2:
        parser.error('--interval 须 >= 2 秒')
    app_id, app_secret = os.getenv('BOT_APP_ID'), os.getenv('BOT_APP_SECRET')
    if not app_id or not app_secret:
        parser.error('请设置 BOT_APP_ID 和 BOT_APP_SECRET')

    known = {}
    token, expires = '', 0.0
    print(f'轮询会议号 {args.meeting_no}；输出是状态变化，不是飞书推送事件。',
          file=sys.stderr, flush=True)
    try:
        while True:
            try:
                if time.monotonic() >= expires:
                    token, expires = get_token(app_id, app_secret)
                for mid in lookup(args.meeting_no, token):
                    meeting = details(mid, token)
                    if str(meeting.get('meeting_no')) != args.meeting_no:
                        continue
                    for label, person_id in observations(known.get(mid), meeting):
                        print(json.dumps({'observed_at': datetime.now(timezone.utc).isoformat(),
                                          'label': label, 'meeting_no': args.meeting_no,
                                          'meeting_id': mid, 'participant_id': person_id},
                                         ensure_ascii=False), flush=True)
                    known[mid] = meeting
            except RuntimeError as exc:
                print(exc, file=sys.stderr, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == '__main__':
    main()
