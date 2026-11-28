"""Admin-site HTTP server: advisor admin-face proxy + tail -f log streaming.

stdlib only, mirroring advisor/bot service conventions: a ThreadingHTTPServer
whose handler is closed over the injected advisor client and logs directory.
"""
import hmac
import html
import json
import logging
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import parse

from .advisor_client import AdvisorAdminClient, AdvisorApiError
from .auth import STATE_TTL_S, AdminSessions, AuthClient, AuthNodeError
from .logtail import LogFollower

LOG = logging.getLogger(__name__)

SERVICES = {'advisor': 'advisor.log', 'bot': 'bot.log', 'stt-server': 'stt.log',
            'gateway': 'gateway.log', 'auth': 'auth.log'}
HEARTBEAT_S = 15.0
MISSING_FILE_NOTE = '（日志文件不存在，等待创建…）'
SESSION_COOKIE = 'admin_session'
STATE_COOKIE = 'admin_oauth_state'

LOGIN_FAILED_PAGE = ('<!doctype html><meta charset="utf-8"><title>登录失败</title>'
                     '<p>登录服务暂时不可用，请稍后<a href="{login}">重试</a>。</p>')
STATE_INVALID_PAGE = ('<!doctype html><meta charset="utf-8"><title>登录状态无效</title>'
                      '<p>登录状态无效或已过期，请<a href="{login}">重新登录</a>。</p>')
DENIED_PAGE = ('<!doctype html><meta charset="utf-8"><title>无权限</title>'
               '<p>账号 <code>{user}</code> 无权访问运维台，请联系管理员。</p>')
LOGGED_OUT_PAGE = ('<!doctype html><meta charset="utf-8"><title>已退出</title>'
                   '<p>已退出登录，<a href="{login}">重新登录</a>。</p>')


def build_server(advisor: AdvisorAdminClient, logs_dir, *, admin_users, auth_client=None,
                 public_prefix='', session_ttl_s=28800, clock=time.time,
                 host='127.0.0.1', port=8390,
                 static_dir: Path | None = None, bootstrap_bytes=65536, poll_s=0.5):
    allowed_users = frozenset(str(user).strip().lower() for user in admin_users
                              if str(user).strip())
    if not allowed_users:
        raise ValueError('admin_users must contain at least one Feishu user_id')
    prefix = '/' + public_prefix.strip('/') if public_prefix.strip('/') else ''
    cookie_path = prefix or '/'
    sessions = AdminSessions(session_ttl_s, clock=clock)
    auth = auth_client if auth_client is not None else AuthClient()
    logs_root = Path(logs_dir)
    static_root: Path = static_dir if static_dir is not None else Path(__file__).parent / 'static'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            message = fmt % args if args else fmt
            path = parse.urlsplit(getattr(self, 'path', '')).path
            if path in ('/login', '/oauth/callback'):
                query = parse.urlsplit(self.path).query
                if query:
                    message = message.replace('?' + query, '')
            LOG.info('%s %s', self.address_string(), message)

        # -- helpers ---------------------------------------------------------

        def _login_url(self):
            return prefix + '/login'

        def _redirect(self, location):
            self.send_response(302)
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()

        def _reply_page(self, status, page):
            body = page.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def _set_cookie(self, name, value, *, max_age):
            self.send_header('Set-Cookie',
                             f'{name}={value}; Path={cookie_path}; HttpOnly; '
                             f'SameSite=Lax; Max-Age={max_age}')

        def _clear_cookie(self, name):
            self._set_cookie(name, '', max_age=0)

        def _cookie_value(self, name):
            raw = self.headers.get('Cookie')
            if not raw:
                return None
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except Exception:
                return None
            morsel = cookie.get(name)
            return morsel.value if morsel is not None else None

        def _current_session(self):
            return sessions.get(self._cookie_value(SESSION_COOKIE))

        def _require_session(self):
            record = self._current_session()
            if record is not None:
                return record
            if parse.urlsplit(self.path).path.startswith('/api/'):
                self._reply_json(401, {'error': {'code': 'unauthorized',
                                                 'message': 'login required'}})
            else:
                self._redirect(self._login_url())
            return None

        def _handle_login(self):
            try:
                started = auth.authorize_start()
            except AuthNodeError as exc:
                LOG.warning('admin login start failed: %s', exc)
                self._reply_page(503, LOGIN_FAILED_PAGE.format(login=self._login_url()))
                return
            self.send_response(302)
            self.send_header('Location', started['authorize_url'])
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self._set_cookie(STATE_COOKIE, started['state'], max_age=STATE_TTL_S)
            self.end_headers()

        def _handle_callback(self, parsed):
            query = parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
            states = query.get('state', [])
            state = states[0] if len(states) == 1 else None
            cookie_state = self._cookie_value(STATE_COOKIE)
            if (not state or not cookie_state
                    or not hmac.compare_digest(state, cookie_state)):
                self._reply_page(400, STATE_INVALID_PAGE.format(login=self._login_url()))
                return
            try:
                status = auth.authorize_status(state)
            except AuthNodeError as exc:
                LOG.warning('admin login status failed: %s', exc)
                self._reply_page(503, LOGIN_FAILED_PAGE.format(login=self._login_url()))
                return
            if status.get('status') != 'completed':
                self._reply_page(400, STATE_INVALID_PAGE.format(login=self._login_url()))
                return
            raw_user = status.get('user')
            user = raw_user if isinstance(raw_user, dict) else {}
            raw_user_id = user.get('user_id')
            user_id = raw_user_id.strip() if isinstance(raw_user_id, str) else ''
            open_id = user.get('open_id')
            name = user.get('name') if isinstance(user.get('name'), str) else None
            if not user_id or user_id.lower() not in allowed_users:
                shown = html.escape(user_id or name or 'unknown')
                LOG.warning('admin login denied for user_id=%s', shown)
                self.send_response(403)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                body = DENIED_PAGE.format(user=shown).encode('utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self._clear_cookie(STATE_COOKIE)
                self.end_headers()
                self.wfile.write(body)
                return
            if not isinstance(open_id, str) or not open_id:
                self._reply_page(400, STATE_INVALID_PAGE.format(login=self._login_url()))
                return
            token = sessions.issue(open_id, user_id, name)
            LOG.info('admin login ok: user_id=%s', user_id)
            self.send_response(302)
            self.send_header('Location', prefix + '/')
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self._set_cookie(SESSION_COOKIE, token, max_age=session_ttl_s)
            self._clear_cookie(STATE_COOKIE)
            self.end_headers()

        def _handle_logout(self):
            token = self._cookie_value(SESSION_COOKIE)
            if token:
                sessions.revoke(token)
            self.send_response(302)
            self.send_header('Location', self._login_url())
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self._clear_cookie(SESSION_COOKIE)
            self._clear_cookie(STATE_COOKIE)
            self.end_headers()

        def _reply_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _reply_file(self, path: Path, content_type):
            try:
                body = path.read_bytes()
            except OSError:
                self._reply_json(404, {'error': {'code': 'not_found', 'message': 'no file'}})
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _discard_body(self):
            length = self.headers.get('Content-Length')
            try:
                remaining = int(length or 0)
            except ValueError:
                remaining = 0
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _read_json(self):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = 0
            if not 0 <= length <= 65536:
                return {}
            try:
                value = json.loads(self.rfile.read(length) or b'{}')
            except ValueError:
                return {}
            return value if isinstance(value, dict) else {}

        def _proxy(self, action):
            try:
                self._reply_json(200, action())
            except AdvisorApiError as exc:
                self._reply_json(exc.status, exc.payload)

        # -- routes ----------------------------------------------------------

        def do_GET(self):
            parsed = parse.urlsplit(self.path)
            if parsed.path == '/healthz':
                self._reply_json(200, {'status': 'ok'})
                return
            if parsed.path == '/login':
                self._handle_login()
                return
            if parsed.path == '/oauth/callback':
                self._handle_callback(parsed)
                return
            if parsed.path == '/logout':
                self._handle_logout()
                return
            record = self._require_session()
            if record is None:
                return
            if parsed.path in ('/', '/index.html'):
                self._reply_file(static_root / 'index.html', 'text/html; charset=utf-8')
            elif parsed.path == '/api/me':
                self._reply_json(200, {'open_id': record['open_id'],
                                       'user_id': record['user_id'],
                                       'name': record['name']})
            elif parsed.path == '/api/sessions':
                self._proxy(advisor.admin_sessions)
            elif parsed.path.startswith('/api/sessions/') and parsed.path.endswith('/transcript'):
                session_id = parsed.path[len('/api/sessions/'):-len('/transcript')]
                self._proxy(lambda: advisor.admin_session_transcript(session_id))
            elif parsed.path.startswith('/api/sessions/') and parsed.path.endswith('/timings'):
                session_id = parsed.path[len('/api/sessions/'):-len('/timings')]
                self._proxy(lambda: advisor.admin_session_timings(session_id))
            elif parsed.path == '/api/supervisors':
                self._proxy(advisor.admin_supervisors)
            elif parsed.path == '/api/config':
                self._proxy(advisor.admin_get_config)
            elif parsed.path == '/api/services':
                self._reply_json(200, {'services': [
                    {'key': key, 'file': name, 'exists': (logs_root / name).exists(),
                     'size': (logs_root / name).stat().st_size
                     if (logs_root / name).exists() else 0}
                    for key, name in SERVICES.items()]})
            elif parsed.path.startswith('/api/logs/') and parsed.path.endswith('/stream'):
                key = parsed.path[len('/api/logs/'):-len('/stream')]
                if key not in SERVICES:
                    self._reply_json(404, {'error': {'code': 'not_found',
                                                     'message': 'unknown service'}})
                else:
                    self._stream_log(logs_root / SERVICES[key])
            else:
                self._reply_json(404, {'error': {'code': 'not_found', 'message': 'unknown path'}})

        def do_POST(self):
            parsed = parse.urlsplit(self.path)
            if self._require_session() is None:
                return
            if parsed.path.startswith('/api/sessions/') and parsed.path.endswith('/end'):
                session_id = parsed.path[len('/api/sessions/'):-len('/end')]
                self._discard_body()
                self._proxy(lambda: advisor.admin_end_session(session_id))
            elif parsed.path == '/api/supervisors':
                self._proxy(lambda: advisor.admin_put_supervisor(**self._read_json()))
            elif parsed.path == '/api/config':
                self._proxy(lambda: advisor.admin_put_config(**self._read_json()))
            else:
                self._discard_body()
                self._reply_json(404, {'error': {'code': 'not_found', 'message': 'unknown path'}})

        # -- SSE log streaming -------------------------------------------------

        def _send_event(self, text: str) -> None:
            data = json.dumps(text, ensure_ascii=False)
            self.wfile.write(f'data: {data}\n\n'.encode())
            self.wfile.flush()

        def _stream_log(self, path: Path):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            follower = LogFollower(path, bootstrap_bytes=bootstrap_bytes)
            reported_missing = False
            last_write = time.monotonic()
            try:
                while True:
                    text = follower.read_new()
                    if text:
                        self._send_event(text)
                        reported_missing = False
                        last_write = time.monotonic()
                    else:
                        if not path.exists() and not reported_missing:
                            reported_missing = True
                            self._send_event(MISSING_FILE_NOTE)
                            last_write = time.monotonic()
                        elif time.monotonic() - last_write >= HEARTBEAT_S:
                            self.wfile.write(b': ping\n\n')
                            self.wfile.flush()
                            last_write = time.monotonic()
                    time.sleep(poll_s)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                follower.close()

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        admin_sessions: AdminSessions

        def handle_error(self, *args):
            pass

    server = Server((host, port), Handler)
    server.admin_sessions = sessions
    return server
