"""Feishu OAuth HTTP transport (ported from legacy OAuthTransport; injectable)."""

import json
import re
from http.client import HTTPException
from urllib import error, parse, request

from .constants import CODE_TOKEN_URL, SCOPES, TOKEN_URL, USER_INFO_URL
from .errors import AuthError
from .storage import digest

_SECRET_TEXT = re.compile(r'[^\x00-\x20\x7f]{1,16000}\Z')


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FeishuTransport:
    def __init__(self, credentials, *, timeout=10, opener=None):
        self.credentials, self.timeout = credentials, timeout
        self.opener = opener or request.build_opener(_NoRedirect()).open

    def _call(self, url, *, payload=None, token=None, timeout=None):
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
                request_timeout = self.timeout if timeout is None else min(self.timeout, max(0.01, timeout))
                response = self.opener(req, timeout=request_timeout)
            except error.HTTPError as exc:
                response = exc
            with response:
                status = getattr(response, 'status', getattr(response, 'code', 200))
                raw = response.read(65536 + 1)
        except (error.URLError, TimeoutError, OSError, HTTPException):
            # A one-use refresh/code may have been consumed remotely. Never replay here.
            raise AuthError('oauth_request_failed', stage=stage) from None
        try:
            result = json.loads(raw) if len(raw) <= 65536 else None
        except (ValueError, UnicodeDecodeError):
            result = None
        code = result.get('code') if isinstance(result, dict) else None
        code = code if type(code) is int else None
        if not 200 <= status < 300 or code not in (None, 0):
            raise AuthError('oauth_token_rejected', http_status=status, code=code, stage=stage)
        if not isinstance(result, dict) or code != 0:
            raise AuthError('oauth_invalid_response', http_status=status, stage=stage)
        return result

    def exchange(self, code, redirect_uri, verifier):
        return self._call(CODE_TOKEN_URL, payload={'grant_type': 'authorization_code',
            'client_id': self.credentials.app_id, 'client_secret': self.credentials.app_secret,
            'code': code, 'redirect_uri': redirect_uri, 'code_verifier': verifier,
            'scope': ' '.join(SCOPES)})

    def refresh(self, refresh_token, *, timeout=None):
        return self._call(TOKEN_URL, payload={'grant_type': 'refresh_token',
            'client_id': self.credentials.app_id, 'client_secret': self.credentials.app_secret,
            'refresh_token': refresh_token, 'scope': ' '.join(SCOPES)}, timeout=timeout)

    def subject(self, token):
        """Return the authorized user's open_id (identity check happens in service)."""
        data = self._call(USER_INFO_URL, token=token).get('data')
        open_id = data.get('open_id') if isinstance(data, dict) else None
        if not isinstance(open_id, str) or not _SECRET_TEXT.fullmatch(open_id) or len(open_id) > 256:
            raise AuthError('oauth_invalid_response')
        # Discard names, email, phone and the raw provider response immediately.
        return open_id

    def subject_hash(self, open_id: str) -> str:
        return digest([self.credentials.app_id, open_id])
