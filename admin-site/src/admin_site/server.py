"""Admin-site HTTP server: advisor admin-face proxy + tail -f log streaming.

stdlib only, mirroring advisor/bot service conventions: a ThreadingHTTPServer
whose handler is closed over the injected advisor client and logs directory.
"""
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import parse

from .advisor_client import AdvisorAdminClient, AdvisorApiError
from .logtail import LogFollower

LOG = logging.getLogger(__name__)

SERVICES = {'advisor': 'advisor.log', 'bot': 'bot.log', 'stt-server': 'stt.log',
            'gateway': 'gateway.log', 'auth': 'auth.log'}
HEARTBEAT_S = 15.0
MISSING_FILE_NOTE = '（日志文件不存在，等待创建…）'


def build_server(advisor: AdvisorAdminClient, logs_dir, *, host='127.0.0.1', port=8390,
                 static_dir: Path | None = None, bootstrap_bytes=65536, poll_s=0.5):
    logs_root = Path(logs_dir)
    static_root: Path = static_dir if static_dir is not None else Path(__file__).parent / 'static'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            LOG.info('%s %s', self.address_string(), fmt % args)

        # -- helpers ---------------------------------------------------------

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

        def _proxy(self, action):
            try:
                self._reply_json(200, action())
            except AdvisorApiError as exc:
                self._reply_json(exc.status, exc.payload)

        # -- routes ----------------------------------------------------------

        def do_GET(self):
            parsed = parse.urlsplit(self.path)
            if parsed.path in ('/', '/index.html'):
                self._reply_file(static_root / 'index.html', 'text/html; charset=utf-8')
            elif parsed.path == '/healthz':
                self._reply_json(200, {'status': 'ok'})
            elif parsed.path == '/api/sessions':
                self._proxy(advisor.admin_sessions)
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
            if parsed.path.startswith('/api/sessions/') and parsed.path.endswith('/end'):
                session_id = parsed.path[len('/api/sessions/'):-len('/end')]
                self._discard_body()
                self._proxy(lambda: advisor.admin_end_session(session_id))
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

        def handle_error(self, *args):
            pass

    return Server((host, port), Handler)
