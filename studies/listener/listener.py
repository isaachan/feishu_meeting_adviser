"""Small, standalone Feishu meeting listening-mode feasibility probe."""

import argparse
import base64
from collections import deque
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import sys
import time
from urllib import error, parse, request


API = 'https://open.feishu.cn'
AUTHORIZE = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
TOKEN = API + '/open-apis/authen/v2/oauth/token'
ACTIVE = API + '/open-apis/vc/v1/bots/user_active_meeting'
EVENTS = API + '/open-apis/vc/v1/bots/events'
SCOPE = 'vc:meeting.meetingevent:read'


class ListenerError(Exception):
    pass


def api_get(url, token, query=None):
    if query:
        url += '?' + parse.urlencode(query)
    req = request.Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    return json_request(req)


def json_request(req):
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        # Never include the response body, request URL, or access token in diagnostics.
        try:
            body = json.loads(exc.read(65537))
            code = body.get('code') if isinstance(body, dict) else None
        except (ValueError, OSError):
            code = None
        raise ListenerError(f'Feishu HTTP {exc.code}, API code={code if type(code) is int else "unknown"}') from None
    except (error.URLError, TimeoutError, ValueError, OSError):
        raise ListenerError('Feishu request or JSON decoding failed') from None
    if not isinstance(result, dict) or result.get('code') != 0:
        code = result.get('code') if isinstance(result, dict) else None
        raise ListenerError(f'Feishu API rejected request (code={code})')
    return result.get('data')


def authorize(app_id, app_secret, port):
    """One-shot local PKCE login; neither token nor authorization code is stored."""
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
            pass  # Callback URL contains a secret code.

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
        print(f'Open this URL in a browser to authorize the meeting listener:\n{login_url}', file=sys.stderr)
        deadline = time.monotonic() + 600
        while outcome['code'] is None and time.monotonic() < deadline:
            server.handle_request()
    if outcome['code'] is None:
        raise ListenerError('User authorization timed out')
    payload = json.dumps({
        'grant_type': 'authorization_code', 'client_id': app_id, 'client_secret': app_secret,
        'code': outcome['code'], 'redirect_uri': redirect, 'code_verifier': verifier,
        'scope': SCOPE,
    }).encode()
    req = request.Request(TOKEN, data=payload, headers={
        'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json',
    })
    data = json_request(req)
    token = data.get('access_token') if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or SCOPE not in data.get('scope', '').split():
        raise ListenerError('User authorization did not return the required scope/token')
    return token


def choose_meeting(token, meeting_no=None):
    data = api_get(ACTIVE, token)
    meetings = data.get('meetings') if isinstance(data, dict) else None
    if not isinstance(meetings, list):
        raise ListenerError('Active meetings response has no meetings list')
    matches = [m for m in meetings if isinstance(m, dict) and
               (meeting_no is None or m.get('meeting_no') == meeting_no)]
    if len(matches) != 1:
        numbers = [str(m.get('meeting_no')) for m in meetings if isinstance(m, dict)]
        raise ListenerError(f'Expected exactly one active meeting; found {len(matches)} matching. '
                            f'Active meeting numbers: {numbers}; use --meeting-no to select.')
    mid = matches[0].get('meeting_id')
    if not isinstance(mid, str) or not mid:
        raise ListenerError('Active meeting has no internal meeting_id')
    return mid


def captions(page, meeting_id):
    """Extract documented activity items plus the flat shape observed by v2."""
    events = page.get('events') if isinstance(page, dict) else None
    if not isinstance(events, list):
        raise ListenerError('Meeting events response has no events list')
    for event in events:
        if not isinstance(event, dict):
            continue
        payload = event.get('payload')
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                raise ListenerError('Invalid event payload JSON') from None
        if not isinstance(payload, dict):
            continue
        if event.get('event_type') == 'meeting.activity':
            if payload.get('activity_event_type') != 'transcript_received':
                continue
        elif event.get('event_type') != 'transcript_received':
            continue
        meeting = payload.get('meeting')
        if isinstance(meeting, dict) and meeting.get('id') not in (None, meeting_id):
            continue
        items = payload.get('transcript_received_items')
        if items is None:
            items = [payload] if isinstance(payload.get('text'), str) else []
        if not isinstance(items, list):
            raise ListenerError('Invalid transcript items')
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get('text'), str):
                continue
            speaker = item.get('speaker')
            speaker = speaker if isinstance(speaker, dict) else {}
            key = (item.get('sentence_id') or f'{event.get("event_id")}:{index}: {item.get("start_time_ms")}')
            yield (str(key), item['text'], speaker.get('user_name') or speaker.get('id') or '未知说话人')


def listen(token, meeting_id, *, interval=2, lookback=180, once=False):
    seen = set()
    order = deque()
    while True:
        end = int(time.time())
        query = {'meeting_id': meeting_id, 'start_time': str(end - lookback),
                 'end_time': str(end), 'page_size': 100}
        cursor = None
        visited = set()
        while True:
            if cursor:
                query['page_token'] = cursor
            page = api_get(EVENTS, token, query)
            for key, text, speaker in captions(page, meeting_id):
                identity = (key, text)
                if identity in seen:
                    continue
                print(f'{speaker}: {text}', flush=True)
                seen.add(identity)
                order.append(identity)
                if len(order) > 20000:
                    seen.remove(order.popleft())
            if not isinstance(page, dict) or page.get('has_more') is not True:
                break
            cursor = page.get('page_token')
            if not isinstance(cursor, str) or not cursor or cursor in visited:
                raise ListenerError('Missing or repeated event pagination cursor')
            visited.add(cursor)
        if once:
            return
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Listen to a meeting without joining a bot; print subtitles to stdout')
    parser.add_argument('--meeting-no', help='select an active meeting by its 9-digit meeting number')
    parser.add_argument('--port', type=int, default=8766, help='local OAuth callback port (default: 8766)')
    parser.add_argument('--interval', type=float, default=2, help='seconds between polling cycles (default: 2)')
    parser.add_argument('--lookback', type=int, default=180, help='seconds of overlapping events per cycle')
    parser.add_argument('--once', action='store_true', help='read one paginated snapshot then exit')
    args = parser.parse_args(argv)
    if args.interval < 1 or not 30 <= args.lookback <= 3600 or not 1024 <= args.port <= 65535:
        parser.error('interval must be >= 1, lookback 30..3600, port 1024..65535')
    if args.meeting_no and (len(args.meeting_no) != 9 or not args.meeting_no.isascii() or not args.meeting_no.isdecimal()):
        parser.error('--meeting-no must be a 9-digit number')
    token = os.environ.get('FEISHU_USER_ACCESS_TOKEN')
    if not token:
        app_id, app_secret = os.environ.get('FEISHU_APP_ID'), os.environ.get('FEISHU_APP_SECRET')
        if not app_id or not app_secret:
            parser.error('set FEISHU_APP_ID and FEISHU_APP_SECRET, or FEISHU_USER_ACCESS_TOKEN')
    try:
        if not token:
            token = authorize(app_id, app_secret, args.port)
        meeting_id = choose_meeting(token, args.meeting_no)
        print('Listening to the selected active meeting; Ctrl-C to stop.', file=sys.stderr)
        listen(token, meeting_id, interval=args.interval, lookback=args.lookback, once=args.once)
    except ListenerError as exc:
        print(f'Listener stopped: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
