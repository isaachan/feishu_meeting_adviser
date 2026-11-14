"""HTTP surface for the inference contract (sections 3-6)."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .scheduler import ExecutionScheduler
from .service import GatewayService, RequestError

JOB_PATH = re.compile(r'^/v1/inference-jobs/([^/]+)$')
CANCEL_PATH = re.compile(r'^/v1/inference-jobs/([^/]+)/cancel$')
MAX_BODY_BYTES = 4 * 1024 * 1024


class GatewayApp:
    """Wires the service and scheduler; the HTTP layer stays stateless."""

    def __init__(self, service: GatewayService, scheduler: ExecutionScheduler | None):
        self.service = service
        self.scheduler = scheduler

    def submit(self, payload) -> tuple[int, dict]:
        return self.service.submit(payload)

    def task(self, request_id: str) -> dict:
        return self.service.task(request_id)

    def cancel(self, request_id: str) -> dict:
        body = self.service.cancel(request_id)
        if self.scheduler is not None:
            self.scheduler.notify_cancel(request_id)
        return body


def create_handler(app: GatewayApp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format, *args):
            return  # Diagnostics stay off the wire; see the operation docs.

        def _send(self, status: int, body: dict):
            raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, status: int, code: str, detail: str = ''):
            body: dict = {'error': {'code': code}}
            if detail:
                body['error']['detail'] = detail
            if status == 429:
                body['error']['retry_after_seconds'] = 1
            self._send(status, body)

        def _read_body(self) -> dict:
            length = self.headers.get('Content-Length')
            if length is None or not length.isdigit() or int(length) < 0:
                raise RequestError(422, 'invalid_request', 'Content-Length required')
            if int(length) > MAX_BODY_BYTES:
                raise RequestError(413, 'payload_too_large')
            raw = self.rfile.read(int(length))
            try:
                value = json.loads(raw)
            except ValueError:
                raise RequestError(422, 'invalid_request', 'body must be JSON') from None
            if not isinstance(value, dict):
                raise RequestError(422, 'invalid_request', 'body must be a JSON object')
            return value

        def do_GET(self):
            try:
                if self.path == '/healthz':
                    self._send(200, {'status': 'ok'})
                    return
                match = JOB_PATH.fullmatch(self.path)
                if match:
                    self._send(200, app.task(match.group(1)))
                    return
                self._error(404, 'not_found')
            except RequestError as exc:
                self._error(exc.status, exc.code, exc.detail)
            except Exception:
                self._error(500, 'server_error')

        def do_POST(self):
            try:
                if self.path == '/v1/context-sessions':
                    body = self._read_body()
                    app.service.prepare(body.get('session_id'), body.get('owner_open_id'))
                    self._send(202, {'state': 'accepted'})
                    return
                if self.path == '/v1/inference-jobs':
                    status, body = app.submit(self._read_body())
                    self._send(status, body)
                    return
                match = CANCEL_PATH.fullmatch(self.path)
                if match:
                    self._send(200, app.cancel(match.group(1)))
                    return
                self._error(404, 'not_found')
            except RequestError as exc:
                self._error(exc.status, exc.code, exc.detail)
            except Exception:
                self._error(500, 'server_error')

    return Handler


def create_server(app: GatewayApp, *, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), create_handler(app))
