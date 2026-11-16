"""Poll ordinary Feishu meetings with a user access token (vc:meeting.meetingevent:read)."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import sys
import time
from urllib import error, parse, request

from poll import API, details, lookup, observations


AUTHORIZE = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
TOKEN = API + '/authen/v2/oauth/token'
SCOPE = 'vc:meeting.meetingevent:read'


class AuthError(RuntimeError):
    pass


def parse_token_response(result):
    """authen v2 puts access_token/scope at the top level, not under data."""
    token = result.get('access_token') if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        keys = sorted(result) if isinstance(result, dict) else 'not a dict'
        raise AuthError(f'换 token 失败：响应无 access_token（keys: {keys}）')
    granted = (result.get('scope') or '').split()
    if SCOPE not in granted:
        print(f'警告：授权 scope 不含 {SCOPE}（实际：{granted or "<空>"}）。'
              '需在应用开通该用户身份权限并发布后重新授权。', file=sys.stderr)
    return token


def exchange(code, verifier, redirect, app_id, app_secret):
    payload = json.dumps({
        'grant_type': 'authorization_code', 'client_id': app_id,
        'client_secret': app_secret, 'code': code, 'redirect_uri': redirect,
        'code_verifier': verifier, 'scope': SCOPE,
    }).encode()
    req = request.Request(TOKEN, data=payload, headers={
        'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json',
    })
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        try:
            body = json.loads(exc.read(65537))
            reason = body.get('error_description') or body.get('error') or ''
        except (ValueError, OSError):
            reason = ''
        raise AuthError(f'换 token 失败：HTTP {exc.code}{("，" + str(reason)[:200]) if reason else ""}') from None
    except (error.URLError, OSError, ValueError) as exc:
        raise AuthError(f'换 token 请求失败：{type(exc).__name__}') from None
    return parse_token_response(result)


def authorize(app_id, app_secret, port):
    """One-shot local PKCE login; token stays in memory only."""
    redirect = f'http://127.0.0.1:{port}/callback'
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    login_url = AUTHORIZE + '?' + parse.urlencode({
        'client_id': app_id, 'response_type': 'code', 'redirect_uri': redirect,
        'scope': SCOPE, 'state': state, 'code_challenge': challenge,
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
                                 secrets.compare_digest(states[0], state) and bool(codes[0]) and
                                 not fields.get('error') and outcome['code'] is None)
                    except ValueError:
                        pass
            if valid:
                outcome['code'] = codes[0]
            body = b'Authorization received; return to the terminal.' if valid else b'Invalid callback.'
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(('127.0.0.1', port), Handler) as server:
        server.timeout = 1
        print(f'请在浏览器打开以下链接，用参加会议的飞书账号授权（10 分钟内有效）：\n{login_url}',
              file=sys.stderr, flush=True)
        deadline = time.monotonic() + 600
        while outcome['code'] is None and time.monotonic() < deadline:
            server.handle_request()
    if outcome['code'] is None:
        raise AuthError('用户授权超时')
    return exchange(outcome['code'], verifier, redirect, app_id, app_secret)


def main():
    parser = argparse.ArgumentParser(
        description='按会议号轮询普通飞书会议（用户身份 vc:meeting.meetingevent:read）')
    parser.add_argument('meeting_no', help='9 位会议号')
    parser.add_argument('--interval', type=float, default=3, help='轮询间隔秒数，默认 3')
    parser.add_argument('--port', type=int, default=8767, help='本机授权回调端口，默认 8767')
    args = parser.parse_args()
    if len(args.meeting_no) != 9 or not args.meeting_no.isascii() or not args.meeting_no.isdecimal():
        parser.error('meeting_no 须为 9 位数字')
    if args.interval < 2:
        parser.error('--interval 须 >= 2 秒')
    if not 1024 <= args.port <= 65535:
        parser.error('--port 须在 1024..65535')
    app_id, app_secret = os.getenv('BOT_APP_ID'), os.getenv('BOT_APP_SECRET')
    if not app_id or not app_secret:
        parser.error('请设置 BOT_APP_ID 和 BOT_APP_SECRET')

    token = os.getenv('FEISHU_USER_ACCESS_TOKEN') or authorize(app_id, app_secret, args.port)
    known = {}
    print(f'轮询会议号 {args.meeting_no}（用户身份）；输出是状态变化，不是飞书推送事件。',
          file=sys.stderr, flush=True)
    try:
        while True:
            try:
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
                if 'HTTP 401' in str(exc):
                    print('用户 token 可能已过期（约 2 小时）；重新运行以重新授权。', file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == '__main__':
    main()
