"""Minimal HTTP clients for the auth and advisor services.

Only the endpoints the replay harness needs are wrapped. Credentials are passed
through but never logged; errors carry the service error code for display.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from urllib import error, parse, request
from uuid import uuid4


class ClientError(RuntimeError):
    def __init__(self, status: int, code: str, message: str,
                 payload: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload or {}
        super().__init__(f'{code}: {message}' if message else code)


class _JsonClient:
    def __init__(self, base_url: str, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = request.build_opener(request.ProxyHandler({}))

    def _request(self, method: str, path: str, *, body=None, form=None,
                 headers: dict | None = None):
        data = None
        merged = dict(headers or {})
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            merged['Content-Type'] = 'application/json'
        elif form is not None:
            data = parse.urlencode(form).encode('utf-8')
            merged['Content-Type'] = 'application/x-www-form-urlencoded'
        req = request.Request(self.base_url + path, data=data, method=method,
                              headers=merged)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' in content_type:
                    return json.loads(raw or b'{}')
                text = raw.decode('utf-8', 'replace')
                try:
                    return json.loads(text)
                except ValueError:
                    return text
        except error.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            code, message = 'http_error', exc.reason or 'request failed'
            failure: dict = {}
            try:
                payload = json.loads(raw or b'{}')
                candidate = payload.get('error') if isinstance(payload, dict) else None
                if isinstance(candidate, dict):
                    failure = candidate
                    code = failure.get('code') or code
                    message = failure.get('message') or message
            except ValueError:
                pass
            raise ClientError(exc.code, str(code), str(message), failure) from None
        except (error.URLError, OSError) as exc:
            raise ClientError(0, 'unreachable', str(exc)) from None
        except ValueError:
            raise ClientError(0, 'bad_response', 'invalid JSON response') from None


def _state(url: object, key: str) -> str:
    if not isinstance(url, str):
        raise ClientError(0, 'bad_response', f'{key} missing')
    state = parse.parse_qs(parse.urlsplit(url).query).get('state', [None])[0]
    if not state:
        raise ClientError(0, 'bad_response', f'{key} missing state')
    return state


class AuthClient(_JsonClient):
    def healthz(self) -> dict:
        return self._request('GET', '/healthz')

    def authorize(self, open_id: str) -> str:
        """Run the stub OAuth callback (code == open_id) for ``open_id``."""
        started = self._request('POST', '/authorize/start',
                                body={'open_id': open_id, 'intent': 'prompt-studio'})
        state = _state((started or {}).get('authorize_url'), 'authorize_url')
        page = self._request(
            'GET', f'/oauth/callback?state={parse.quote(state)}'
                   f'&code={parse.quote(open_id)}')
        if isinstance(page, str) and '授权成功' not in page:
            raise ClientError(0, 'authorize_failed', page[:200])
        return page if isinstance(page, str) else ''

    def submit_provider_token(self, open_id: str, provider: str, token: str) -> str:
        if provider not in ('nsdp', 'jira'):
            raise ValueError('provider must be nsdp or jira')
        started = self._request('POST', '/token-submissions/start',
                                body={'open_id': open_id, 'provider': provider})
        state = _state((started or {}).get('submit_url'), 'submit_url')
        page = self._request('POST', f'/{provider}/submit?state={parse.quote(state)}',
                             form={'token': token})
        if isinstance(page, str) and '已保存' not in page:
            raise ClientError(0, 'token_rejected', page[:200])
        return page if isinstance(page, str) else ''


@dataclass(frozen=True)
class RecorderArgs:
    session: str
    token: str
    event: str
    url: str


class AdvisorClient(_JsonClient):
    def healthz(self) -> dict:
        return self._request('GET', '/healthz')

    def create_session(self, owner_open_id: str, meeting_no: str) -> dict:
        return self._request(
            'POST', '/sessions',
            body={'owner_open_id': owner_open_id, 'meeting_ref': {'no': meeting_no}},
            headers={'X-Idempotency-Key': uuid4().hex})

    def status(self, session_id: str) -> dict:
        return self._request('GET', f'/sessions/{session_id}/status')

    def transcript(self, session_id: str) -> dict:
        return self._request('GET', f'/admin/sessions/{session_id}/transcript')

    def timings(self, session_id: str) -> dict:
        return self._request('GET', f'/admin/sessions/{session_id}/timings')

    def end_session(self, session_id: str) -> dict:
        return self._request('POST', f'/admin/sessions/{session_id}/end')

    @staticmethod
    def recorder_args(response: dict) -> RecorderArgs:
        args = response.get('recorder_command_args') if isinstance(response, dict) else None
        if not isinstance(args, dict):
            raise ClientError(0, 'bad_response', 'recorder_command_args missing')
        session = args.get('session')
        token = args.get('token')
        event = args.get('event')
        url = args.get('url')
        if not (isinstance(session, str) and session
                and isinstance(token, str) and token
                and isinstance(event, str) and event
                and isinstance(url, str) and url):
            raise ClientError(0, 'bad_response', 'incomplete recorder_command_args')
        return RecorderArgs(session=session, token=token, event=event, url=url)
