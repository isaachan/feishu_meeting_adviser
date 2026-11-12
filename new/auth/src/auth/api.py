"""HTTP surface: internal API (localhost) + OAuth callback (via reverse proxy).

Implements CONTRACT_AUTH.md: POST /authorize/start, GET /credentials/{open_id}/token,
GET /oauth/callback, GET /healthz. No request logging: query strings carry
authorization codes and states.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse

from .errors import AuthError
from .service import AuthService

_ERROR_MAP = {
    'oauth_not_authorized': (404, 'not_authorized'),
    'oauth_reauthorization_required': (409, 'reauthorization_required'),
    'oauth_request_failed': (503, 'upstream_unavailable'),
    'oauth_busy': (503, 'busy'),
    'oauth_invalid_open_id': (400, 'bad_message'),
    'oauth_invalid_callback': (400, 'bad_message'),
    'nsdp_invalid_token': (400, 'bad_message'),
    'nsdp_invalid_provider': (400, 'bad_message'),
}

_SUCCESS_PAGE = ('<!doctype html><meta charset="utf-8"><title>授权成功</title>'
                 '<p>授权成功。请回到飞书，重新发送刚才的指令{intent}。</p>')
_DENIED_PAGE = ('<!doctype html><meta charset="utf-8"><title>已取消</title>'
                '<p>已取消授权，未保存任何凭据。可回到飞书重新发起。</p>')
_INVALID_PAGE = ('<!doctype html><meta charset="utf-8"><title>链接无效</title>'
                 '<p>授权链接无效或已过期，请回到飞书重新发起。</p>')
_FAILED_PAGE = ('<!doctype html><meta charset="utf-8"><title>授权失败</title>'
                '<p>授权失败（阶段：{stage}）。请回到飞书重新发起。</p>')
_TOKEN_FORM_PAGE = ('<!doctype html><meta charset="utf-8"><title>提交 NSDP token</title>'
                    '<p>请粘贴你的 NSDP token 并提交（链接 10 分钟内有效，一次性）。'
                    'token 仅保存在会参谋云端、只用于代你查询版本计划，不会出现在聊天里。</p>'
                    '<form method="post" autocomplete="off">'
                    '<input type="password" name="token" maxlength="512" required '
                    'placeholder="NSDP token">'
                    '<button type="submit">提交</button></form>')
_TOKEN_SAVED_PAGE = ('<!doctype html><meta charset="utf-8"><title>已保存</title>'
                     '<p>NSDP token 已保存。请回到飞书，重新发送刚才的指令。</p>')
_TOKEN_INVALID_PAGE = ('<!doctype html><meta charset="utf-8"><title>提交无效</title>'
                       '<p>token 格式无效或链接已失效，未保存任何凭据。请回到飞书重新发起。</p>')


def _api_error(exc: AuthError):
    status, code = _ERROR_MAP.get(exc.reason, (500, 'internal'))
    return status, {'error': {'code': code, 'message': 'details hidden'}}


class _Handler(BaseHTTPRequestHandler):
    service: 'AuthService | None' = None  # injected by build_server

    def log_message(self, *args):
        pass  # Never log request lines: they may carry codes/states.

    def _service(self) -> AuthService:
        service = self.service
        assert service is not None
        return service

    def _reply_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _reply_page(self, status, html):
        body = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _read_json(self):
        raw_length = self.headers.get('Content-Length')
        if raw_length is None:
            raise AuthError('oauth_invalid_callback')
        try:
            length = int(raw_length)
        except ValueError:
            raise AuthError('oauth_invalid_callback') from None
        if not 0 <= length <= 8192:
            raise AuthError('oauth_invalid_callback')
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise AuthError('oauth_invalid_callback') from None
        if not isinstance(value, dict):
            raise AuthError('oauth_invalid_callback')
        return value

    def _read_form_token(self):
        """Single urlencoded field ``token`` (the NSDP submit form body)."""
        raw_length = self.headers.get('Content-Length')
        try:
            length = int(raw_length or '0')
        except ValueError:
            raise AuthError('nsdp_invalid_token') from None
        if not 0 <= length <= 8192:
            raise AuthError('nsdp_invalid_token')
        raw = self.rfile.read(length)
        try:
            fields = parse.parse_qs(raw.decode('utf-8'),
                                    keep_blank_values=True, max_num_fields=8)
        except (ValueError, UnicodeDecodeError):
            raise AuthError('nsdp_invalid_token') from None
        values = fields.get('token', [])
        if set(fields) != {'token'} or len(values) != 1:
            raise AuthError('nsdp_invalid_token')
        return values[0]

    def do_GET(self):
        parsed = parse.urlsplit(self.path)
        if parsed.path == '/healthz':
            self._reply_json(200, {'status': 'ok'})
            return
        if parsed.path == '/oauth/callback':
            try:
                outcome = self._service().callback(parsed.query)
            except AuthError:
                outcome = {'outcome': 'invalid'}
            if outcome['outcome'] == 'success':
                intent = outcome.get('intent') or ''
                hint = f'（{intent}）' if intent else ''
                self._reply_page(200, _SUCCESS_PAGE.format(intent=hint))
            elif outcome['outcome'] == 'denied':
                self._reply_page(403, _DENIED_PAGE)
            elif outcome['outcome'] == 'failed':
                self._reply_page(400, _FAILED_PAGE.format(stage=outcome.get('stage', 'validation')))
            else:
                self._reply_page(400, _INVALID_PAGE)
            return
        if parsed.path == '/nsdp/submit':
            query = parse.parse_qs(parsed.query, keep_blank_values=True)
            state = query.get('state', [None])[0]
            try:
                self._service().nsdp_peek(state)
            except AuthError:
                self._reply_page(400, _INVALID_PAGE)
                return
            self._reply_page(200, _TOKEN_FORM_PAGE)
            return
        if parsed.path.startswith('/credentials/') and parsed.path.endswith('/token'):
            open_id = parsed.path[len('/credentials/'):-len('/token')]
            query = parse.parse_qs(parsed.query, keep_blank_values=True)
            rejected = query.get('rejected', [None])[0]
            provider = query.get('provider', ['feishu'])[0] or 'feishu'
            try:
                if provider == 'nsdp':
                    self._reply_json(200, self._service().get_nsdp_token(open_id))
                elif provider == 'feishu':
                    self._reply_json(200, self._service().get_valid_token(open_id, rejected_token=rejected))
                else:
                    raise AuthError('nsdp_invalid_provider')
            except AuthError as exc:
                status, payload = _api_error(exc)
                self._reply_json(status, payload)
            return
        self._reply_json(404, {'error': {'code': 'not_found', 'message': 'unknown path'}})

    def do_POST(self):
        parsed = parse.urlsplit(self.path)
        if parsed.path == '/authorize/start':
            try:
                body = self._read_json()
                result = self._service().authorize_start(body.get('open_id'), body.get('intent') or '')
            except AuthError as exc:
                status, payload = _api_error(exc)
                self._reply_json(status, payload)
                return
            self._reply_json(200, result)
            return
        if parsed.path == '/token-submissions/start':
            try:
                body = self._read_json()
                if body.get('provider') != 'nsdp':
                    raise AuthError('nsdp_invalid_provider')
                result = self._service().nsdp_submission_start(body.get('open_id'))
            except AuthError as exc:
                status, payload = _api_error(exc)
                self._reply_json(status, payload)
                return
            self._reply_json(200, result)
            return
        if parsed.path == '/nsdp/submit':
            query = parse.parse_qs(parsed.query, keep_blank_values=True)
            state = query.get('state', [None])[0]
            try:
                outcome = self._service().nsdp_submit(state, self._read_form_token())
            except AuthError as exc:
                page = _TOKEN_INVALID_PAGE if exc.reason in ('nsdp_invalid_token',
                                                             'oauth_state_used',
                                                             'oauth_unknown_state') else _INVALID_PAGE
                self._reply_page(400, page)
                return
            assert outcome['open_id']
            self._reply_page(200, _TOKEN_SAVED_PAGE)
            return
        if parsed.path.startswith('/credentials/') and parsed.path.endswith('/invalidate'):
            open_id = parsed.path[len('/credentials/'):-len('/invalidate')]
            try:
                body = self._read_json()
                if body.get('provider') != 'nsdp':
                    raise AuthError('nsdp_invalid_provider')
                result = self._service().invalidate_nsdp(open_id)
            except AuthError as exc:
                status, payload = _api_error(exc)
                self._reply_json(status, payload)
                return
            self._reply_json(200, result)
            return
        self._reply_json(404, {'error': {'code': 'not_found', 'message': 'unknown path'}})


class AuthServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *args):
        pass  # No tracebacks that might include request data.


def build_server(service: AuthService, host='127.0.0.1', port=8381) -> AuthServer:
    handler = type('BoundHandler', (_Handler,), {'service': service})
    return AuthServer((host, port), handler)


def serve_forever(server: AuthServer):
    server.serve_forever()
