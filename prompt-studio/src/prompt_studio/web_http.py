"""Minimal HTTP server and JSON routes for the prompt-studio web UI."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import traceback
from urllib.parse import unquote, urlsplit

from .clients import ClientError
from .replay import ReplayError
from .stack import Stack, StackError
from .transcript import TranscriptError
from .web import WebError
from .web_app import StudioApp

MAX_BODY = 2_100_000
STATIC = Path(__file__).resolve().parent / 'static'
ALLOWED_BINDS = ('127.0.0.1', '0.0.0.0')


def validate_bind(host: str) -> str:
    if host not in ALLOWED_BINDS:
        raise ValueError('host 只能是 127.0.0.1（默认）或 0.0.0.0（内网）')
    return host


def _primary_ipv4() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            address = sock.getsockname()[0]
    except OSError:
        return None
    return address if address and not address.startswith('127.') else None


def make_handler(app: StudioApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'prompt-studio/0.1'
        protocol_version = 'HTTP/1.1'

        def do_GET(self):  # noqa: N802
            self._dispatch('GET')

        def do_POST(self):  # noqa: N802
            self._dispatch('POST')

        def do_PUT(self):  # noqa: N802
            self._dispatch('PUT')

        def log_message(self, format, *args):  # noqa: A002
            pass

        def _dispatch(self, method: str):
            try:
                self._route(method)
            except WebError as exc:
                self._json(exc.status, {'error': exc.code,
                                        'message': exc.message})
            except TranscriptError as exc:
                self._json(400, {'error': 'bad_transcript',
                                 'message': str(exc)})
            except ReplayError as exc:
                self._json(409, {'error': 'replay', 'message': str(exc)})
            except StackError as exc:
                self._json(500, {'error': 'stack', 'message': str(exc)})
            except ClientError as exc:
                self._json(502, {'error': 'upstream', 'message': str(exc)})
            except OSError as exc:
                self._json(500, {'error': 'io', 'message': str(exc)})
            except Exception as exc:  # noqa: BLE001 - return a JSON error
                traceback.print_exc(file=sys.stderr)
                self._json(500, {'error': 'internal',
                                 'message': f'{type(exc).__name__}: {exc}'})

        def _route(self, method: str):
            path = unquote(urlsplit(self.path).path)
            if method == 'GET':
                if path in ('/', '/index.html'):
                    return self._static()
                if path == '/api/health':
                    return self._json(200, app.health())
                if path == '/api/config':
                    return self._json(200, app.config())
                if path == '/api/prompts':
                    return self._json(200, app.prompts())
                if path == '/api/run':
                    return self._json(200, app.runner.snapshot())
                raise WebError(404, 'not_found', 'not found')
            if method == 'POST':
                if path == '/api/parse':
                    return self._json(200, app.parse(self._body()))
                if path == '/api/run/start':
                    return self._json(201, app.start_run(self._body()))
                if path == '/api/run/next':
                    return self._json(200, app.runner.next_step())
                if path == '/api/run/finish':
                    return self._json(200, app.runner.finish())
                if path == '/api/config':
                    return self._json(200, app.set_model(self._body()))
                raise WebError(404, 'not_found', 'not found')
            if method == 'PUT':
                prefix = '/api/prompts/'
                if path.startswith(prefix) and len(path) > len(prefix):
                    name = path[len(prefix):]
                    return self._json(200, app.write_prompt(name,
                                                            self._body()))
                raise WebError(404, 'not_found', 'not found')
            raise WebError(405, 'method_not_allowed', 'method not allowed')

        def _body(self):
            raw_length = self.headers.get('Content-Length')
            if raw_length is None:
                return {}
            try:
                length = int(raw_length)
            except ValueError:
                raise WebError(400, 'bad_request', 'Content-Length 无效') from None
            if length < 0 or length > MAX_BODY:
                raise WebError(413, 'too_large', '请求体过大')
            if length == 0:
                return {}
            content_type = self.headers.get('Content-Type', '')
            if content_type.split(';')[0].strip().lower() != 'application/json':
                raise WebError(415, 'unsupported_media_type',
                               'Content-Type 必须是 application/json')
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode('utf-8')) if raw else {}
            except (UnicodeDecodeError, ValueError):
                raise WebError(400, 'bad_json', '请求体不是合法 JSON') from None
            if not isinstance(value, dict):
                raise WebError(400, 'bad_json', '请求体必须是 JSON 对象')
            return value

        def _static(self):
            target = STATIC / 'index.html'
            try:
                body = target.read_bytes()
            except OSError:
                raise WebError(500, 'static', 'UI 静态文件缺失') from None
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            if status >= 400:
                self.close_connection = True
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            if self.close_connection:
                self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(stack: Stack, host: str, port: int, prompt_dir: str | Path,
          owner: str):
    host = validate_bind(host)
    if not 0 <= port <= 65535:
        raise ValueError('port 必须在 0-65535')
    app = StudioApp(stack, prompt_dir, owner)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    actual_port = server.server_address[1]
    print(f'prompt-studio: listening on {host}:{actual_port}')
    if host == '0.0.0.0':
        address = _primary_ipv4()
        target = (f'{address}:{actual_port}' if address
                  else f'<server-ip>:{actual_port}')
        print(f'prompt-studio: http://{target}')
        print('警告: UI 未认证，已绑定所有网卡；仅限可信内网，并配防火墙。')
    else:
        print(f'prompt-studio: http://{host}:{actual_port}')
        print('远端可通过 SSH 隧道访问。')
    print('按 Ctrl-C 停止。')
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print('\nprompt-studio: stopped')
    finally:
        app.close()
        server.server_close()
