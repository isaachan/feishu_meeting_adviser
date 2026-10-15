"""Local user OAuth for material reads, separate from the VC application identity.

Only authorization/token rotation state is stored here. Codes, callback URLs,
provider errors and tokens are never written to diagnostics or stdout.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from urllib import error, parse, request

from .infrastructure.errors import LifecycleError, LockBusy
from .infrastructure.file_lock import FileLock
from .models import digest
from .storage import StorageError, private_directory


AUTHORIZE_URL = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
# The documented v1 authorization page currently binds PKCE to this exchange endpoint.
# v3 refresh explicitly accepts tokens issued by v2. Do not retry consumed codes across versions.
CODE_TOKEN_URL = 'https://open.feishu.cn/open-apis/authen/v2/oauth/token'
TOKEN_URL = 'https://accounts.feishu.cn/oauth/v3/token'
USER_INFO_URL = 'https://open.feishu.cn/open-apis/authen/v1/user_info'
DEFAULT_PORT = 8765
CALLBACK_PATH = '/oauth/callback'
SCOPES = ('calendar:calendar:read', 'calendar:calendar.event:read',
          'docx:document:readonly', 'wiki:node:read', 'offline_access')
MAX_FILE_BYTES = 65_536
_SECRET_TEXT = re.compile(r'[^\x00-\x20\x7f]{1,16000}\Z')
_CODE = re.compile(r'[A-Za-z0-9_-]{1,4096}\Z')
_REASONS = {'oauth_not_authorized', 'oauth_reauthorization_required', 'oauth_busy',
    'oauth_invalid_response', 'oauth_request_failed', 'oauth_token_rejected',
    'oauth_missing_scopes', 'oauth_excess_scopes', 'oauth_denied', 'oauth_timeout',
    'oauth_invalid_callback', 'oauth_invalid_local_file', 'oauth_local_write_failed'}


class OAuthError(Exception):
    def __init__(self, reason, *, http_status=None, code=None, stage=None):
        self.reason = reason if reason in _REASONS else 'oauth_invalid_response'
        self.http_status, self.code = http_status, code
        self.stage = stage if stage in ('code_exchange', 'refresh', 'user_identity') else None
        super().__init__(f'{self.reason} (stage={self.stage}, HTTP={http_status}, code={code}); details hidden')


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OAuthTransport:
    def __init__(self, credentials, *, timeout=10, opener=None):
        self.credentials, self.timeout = credentials, timeout
        self.opener = opener or request.build_opener(_NoRedirect()).open

    def _call(self, url, *, payload=None, token=None):
        if url not in (CODE_TOKEN_URL, TOKEN_URL, USER_INFO_URL) or (url != USER_INFO_URL) != (payload is not None):
            raise ValueError('unsupported OAuth request')
        stage = 'code_exchange' if url == CODE_TOKEN_URL else 'refresh' if url == TOKEN_URL else 'user_identity'
        headers = {'Accept': 'application/json'}
        body = None
        if payload is not None:
            if url == CODE_TOKEN_URL:
                body = json.dumps(payload).encode('utf-8')
                headers['Content-Type'] = 'application/json; charset=utf-8'
            else:
                body = parse.urlencode(payload).encode('ascii')
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if token:
            headers['Authorization'] = 'Bearer ' + token
        req = request.Request(url, data=body, headers=headers, method='POST' if body else 'GET')
        try:
            try:
                response = self.opener(req, timeout=self.timeout)
            except error.HTTPError as exc:
                response = exc
            with response:
                status = getattr(response, 'status', getattr(response, 'code', 200))
                raw = response.read(MAX_FILE_BYTES + 1)
        except (error.URLError, TimeoutError, OSError, HTTPException):
            # A one-use refresh/code may have been consumed remotely. Never replay here.
            raise OAuthError('oauth_request_failed', stage=stage) from None
        try:
            result = json.loads(raw) if len(raw) <= MAX_FILE_BYTES else None
        except (ValueError, UnicodeDecodeError):
            result = None
        code = result.get('code') if isinstance(result, dict) else None
        code = code if type(code) is int else None
        if not 200 <= status < 300 or code not in (None, 0):
            raise OAuthError('oauth_token_rejected', http_status=status, code=code, stage=stage)
        if not isinstance(result, dict) or code != 0:
            raise OAuthError('oauth_invalid_response', http_status=status, stage=stage)
        return result

    def exchange(self, code, redirect_uri, verifier):
        return self._call(CODE_TOKEN_URL, payload={'grant_type': 'authorization_code',
            'client_id': self.credentials.app_id, 'client_secret': self.credentials.app_secret,
            'code': code, 'redirect_uri': redirect_uri, 'code_verifier': verifier,
            'scope': ' '.join(SCOPES)})

    def refresh(self, refresh_token):
        return self._call(TOKEN_URL, payload={'grant_type': 'refresh_token',
            'client_id': self.credentials.app_id, 'client_secret': self.credentials.app_secret,
            'refresh_token': refresh_token, 'scope': ' '.join(SCOPES)})

    def subject(self, token):
        data = self._call(USER_INFO_URL, token=token).get('data')
        open_id = data.get('open_id') if isinstance(data, dict) else None
        if not isinstance(open_id, str) or not _SECRET_TEXT.fullmatch(open_id) or len(open_id) > 256:
            raise OAuthError('oauth_invalid_response')
        # Discard names, email, phone and the raw provider response immediately.
        return digest([self.credentials.app_id, open_id])


class OAuthManager:
    def __init__(self, credentials, directory, *, transport=None, clock=time.time):
        self.credentials, self.clock = credentials, clock
        self.directory = Path(directory) / 'oauth'
        self.app_key = digest(credentials.app_id)
        self.path = self.directory / (self.app_key + '.json')
        self.transport = transport or OAuthTransport(credentials)

    @contextmanager
    def locked(self):
        private_directory(self.directory.parent)
        private_directory(self.directory)
        try:
            with FileLock(self.directory / (self.app_key + '.lock')):
                yield
        except LockBusy:
            raise OAuthError('oauth_busy') from None
        except LifecycleError:
            raise OAuthError('oauth_invalid_local_file') from None

    def _check_file(self):
        if self.path.is_symlink() or self.path.exists() and (
                not self.path.is_file() or self.path.stat().st_mode & 0o077):
            raise OAuthError('oauth_invalid_local_file')

    def _read(self):
        self._check_file()
        if not self.path.exists():
            raise OAuthError('oauth_not_authorized')
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as source:
                raw = source.read(MAX_FILE_BYTES + 1)
            value = json.loads(raw) if len(raw) <= MAX_FILE_BYTES else None
        except (OSError, ValueError, UnicodeDecodeError):
            raise OAuthError('oauth_invalid_local_file') from None
        if (not isinstance(value, dict) or value.get('version') != 1 or value.get('app_key') != self.app_key
                or value.get('state') not in ('ready', 'refreshing', 'reauthorization_required')):
            raise OAuthError('oauth_invalid_local_file')
        return value

    def _write(self, value):
        self._check_file()
        raw = (json.dumps(value, separators=(',', ':')) + '\n').encode()
        if len(raw) > MAX_FILE_BYTES:
            raise OAuthError('oauth_invalid_response')
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix='.token-', dir=self.directory)
            with os.fdopen(fd, 'wb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise OAuthError('oauth_local_write_failed') from None
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _token_record(self, result, *, started, subject_hash):
        for key in ('access_token', 'refresh_token'):
            if not isinstance(result.get(key), str) or not _SECRET_TEXT.fullmatch(result[key]):
                raise OAuthError('oauth_invalid_response')
        for key in ('expires_in', 'refresh_token_expires_in'):
            if type(result.get(key)) is not int or not 0 < result[key] <= 366 * 86400:
                raise OAuthError('oauth_invalid_response')
        if result.get('token_type') != 'Bearer' or not isinstance(result.get('scope'), str):
            raise OAuthError('oauth_invalid_response')
        scopes = set(result['scope'].split())
        if not set(SCOPES) <= scopes:
            raise OAuthError('oauth_missing_scopes')
        if scopes - set(SCOPES) - {'user_profile'}:
            raise OAuthError('oauth_excess_scopes')
        return {'version': 1, 'app_key': self.app_key, 'state': 'ready', 'subject_hash': subject_hash,
            'access_token': result['access_token'], 'refresh_token': result['refresh_token'],
            'expires_at': started + result['expires_in'],
            'refresh_expires_at': started + result['refresh_token_expires_in'],
            'scopes': sorted(scopes), 'refresh_margin': min(300, result['expires_in'] / 2)}

    def authorize(self, code, redirect_uri, verifier):
        # Caller holds the lease throughout login; a consumer cannot see half-installed credentials.
        started = self.clock()
        result = self.transport.exchange(code, redirect_uri, verifier)
        value = self._token_record(result, started=started, subject_hash=None)
        value['subject_hash'] = self.transport.subject(value['access_token'])
        self._write(value)
        return {'authorized': True, 'identity': 'user', 'scopes': list(SCOPES)}

    def access_token(self, *, rejected_token=None):
        with self.locked():
            value = self._read()
            if value['state'] != 'ready':
                raise OAuthError('oauth_reauthorization_required')
            try:
                valid_time = (type(value['expires_at']) in (int, float) and math.isfinite(value['expires_at'])
                    and type(value['refresh_margin']) in (int, float) and math.isfinite(value['refresh_margin']))
                if not valid_time or not set(SCOPES) <= set(value['scopes']) or not _SECRET_TEXT.fullmatch(value['access_token']):
                    raise ValueError()
                reusable = self.clock() < value['expires_at'] - value['refresh_margin']
                if reusable and (rejected_token is None or value['access_token'] != rejected_token):
                    return value['access_token']
                if (not _SECRET_TEXT.fullmatch(value['refresh_token'])
                        or type(value['refresh_expires_at']) not in (int, float)
                        or not math.isfinite(value['refresh_expires_at'])):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise OAuthError('oauth_invalid_local_file') from None
            marker = {'version': 1, 'app_key': self.app_key, 'state': 'refreshing',
                      'subject_hash': value.get('subject_hash')}
            if self.clock() >= value['refresh_expires_at']:
                self._write(marker | {'state': 'reauthorization_required'})
                raise OAuthError('oauth_reauthorization_required')
            # Commit before the one-use POST. A crash at any later point forbids reuse.
            self._write(marker)
            started = self.clock()
            try:
                result = self.transport.refresh(value['refresh_token'])
                replacement = self._token_record(result, started=started, subject_hash=value.get('subject_hash'))
                if replacement['refresh_token'] == value['refresh_token']:
                    raise OAuthError('oauth_invalid_response')
                self._write(replacement)
            except (OAuthError, OSError):
                self._write(marker | {'state': 'reauthorization_required'})
                raise
            return replacement['access_token']

    def status(self):
        with self.locked():
            try:
                value = self._read()
            except OAuthError as exc:
                if exc.reason == 'oauth_not_authorized':
                    return {'authorized': False, 'state': 'not_authorized'}
                raise
            return {'authorized': value['state'] == 'ready', 'state': value['state'],
                    'identity': 'user', 'scopes': [scope for scope in SCOPES if scope in value.get('scopes', [])]}

    def subject_hash(self):
        with self.locked():
            value = self._read()
            if value['state'] != 'ready' or not isinstance(value.get('subject_hash'), str):
                raise OAuthError('oauth_reauthorization_required')
            return value['subject_hash']

    def logout(self):
        with self.locked():
            self._check_file()
            self.path.unlink(missing_ok=True)
        return {'authorized': False, 'state': 'not_authorized'}


def authorization_parameters(client_id, port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise OAuthError('oauth_invalid_callback')
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    redirect_uri = f'http://127.0.0.1:{port}{CALLBACK_PATH}'
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
    url = AUTHORIZE_URL + '?' + parse.urlencode({'client_id': client_id, 'response_type': 'code',
        'redirect_uri': redirect_uri, 'scope': ' '.join(SCOPES), 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'consent'})
    return {'url': url, 'redirect_uri': redirect_uri, 'state': state, 'verifier': verifier}


def callback_code(path, expected_state):
    if len(path) > 8192:
        raise OAuthError('oauth_invalid_callback')
    parsed = parse.urlsplit(path)
    if parsed.path != CALLBACK_PATH or parsed.scheme or parsed.netloc or parsed.fragment:
        raise OAuthError('oauth_invalid_callback')
    try:
        params = parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
    except ValueError:
        raise OAuthError('oauth_invalid_callback') from None
    states = params.get('state', [])
    if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
        raise OAuthError('oauth_invalid_callback')
    if 'error' in params:
        if params.get('error') == ['access_denied'] and 'code' not in params:
            raise OAuthError('oauth_denied')
        raise OAuthError('oauth_invalid_callback')
    codes = params.get('code', [])
    if len(codes) != 1 or not _CODE.fullmatch(codes[0]):
        raise OAuthError('oauth_invalid_callback')
    return codes[0]


def login(manager, *, port=DEFAULT_PORT, timeout=600, notify=print):
    parameters = authorization_parameters(manager.credentials.app_id, port)
    result = {'finished': False, 'error': None, 'success': None}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # The default logger includes the callback code/query.

        def reply(self, status, message, *, location=None):
            body = message.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            if location:
                self.send_header('Location', location)
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # Credentials were already saved; client disconnect must not repeat exchange.

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}' or result['finished']:
                self.reply(400, 'Invalid local authorization request.')
                return
            if self.path == '/':
                self.reply(302, 'Continue in Feishu.', location=parameters['url'])
                return
            try:
                code = callback_code(self.path, parameters['state'])
            except OAuthError as exc:
                if exc.reason == 'oauth_denied':
                    result.update(finished=True, error=exc)
                    self.reply(403, 'Authorization declined. No credentials saved.')
                else:
                    self.reply(400, 'Invalid authorization callback. Retry from the original login link.')
                return
            result['finished'] = True
            try:
                result['success'] = manager.authorize(code, parameters['redirect_uri'], parameters['verifier'])
            except (OAuthError, StorageError, OSError) as exc:
                result['error'] = exc
                if isinstance(exc, OAuthError):
                    stage = exc.stage or 'validation'
                    code = str(exc.code) if exc.code is not None else 'unavailable'
                    self.reply(400, f'Authorization failed at {stage} (provider code: {code}). Check the local terminal for a safe error.')
                else:
                    self.reply(400, 'Local credential save failed. Check the local terminal for a safe error.')
            else:
                self.reply(200, 'User authorization saved locally. You may close this page.')

    class Server(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(2)
            return connection, address
        def handle_error(self, *args):
            pass  # No request tracebacks containing authorization parameters.

    with manager.locked(), Server(('127.0.0.1', port), Handler) as server:
        server.timeout = 0.5
        deadline = time.monotonic() + timeout
        notify(f'Add this exact redirect URL to the Feishu app: {parameters["redirect_uri"]}')
        notify(f'Open http://127.0.0.1:{port}/ and complete user authorization. Waiting locally.')
        while not result['finished'] and time.monotonic() < deadline:
            server.handle_request()
        if result['error']:
            raise result['error']
        if not result['finished']:
            raise OAuthError('oauth_timeout')
        return result['success']


def main(argv=None):
    from .config import ConfigError, DEFAULT_CONFIG, load_config
    parser = argparse.ArgumentParser(description='Local Feishu user OAuth for read-only meeting materials')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest='command', required=True)
    authorize = commands.add_parser('login')
    authorize.add_argument('--port', type=int, default=DEFAULT_PORT)
    authorize.add_argument('--timeout', type=int, choices=range(30, 1801), default=600, metavar='30..1800')
    commands.add_parser('status')
    commands.add_parser('logout', help='forget local credentials; remote grants are managed in Feishu')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        manager = OAuthManager(config.credentials, config.data_directory,
            transport=OAuthTransport(config.credentials, timeout=config.timeout_seconds))
        if args.command == 'login':
            result = login(manager, port=args.port, timeout=args.timeout, notify=lambda value: print(value, flush=True))
        elif args.command == 'status':
            result = manager.status()
        else:
            result = manager.logout()
        print(json.dumps(result), flush=True)
        return 0
    except (OAuthError, ConfigError, StorageError) as exc:
        print('OAuth stopped: ' + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print('OAuth stopped: local socket/file error; details hidden', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
