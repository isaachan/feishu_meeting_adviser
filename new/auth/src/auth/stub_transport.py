"""Stub OAuth transport for the integration bed (skip browser + Feishu).

The authorization ``code`` IS the expected open_id: the stub mints
``stub-<open_id>`` access tokens and ``subject()`` decodes them back, so the
production callback path (exchange -> subject == state-bound open_id) runs
unchanged. Never enable against real credentials.
"""
import time


class StubTransport:
    exchange_calls = 0
    refresh_calls = 0

    def exchange(self, code, redirect_uri, verifier):
        StubTransport.exchange_calls += 1
        return {'access_token': f'stub-{code}', 'refresh_token': f'stub-r-{code}',
                'token_type': 'Bearer', 'expires_in': 7200,
                'refresh_token_expires_in': 2592000,
                'scope': 'calendar:calendar:read calendar:calendar.event:read offline_access'}

    def refresh(self, refresh_token, *, timeout=None):
        StubTransport.refresh_calls += 1
        return {'access_token': f'stub-refreshed-{StubTransport.refresh_calls}',
                'refresh_token': f'stub-rr-{StubTransport.refresh_calls}',
                'token_type': 'Bearer', 'expires_in': 7200,
                'refresh_token_expires_in': 2592000,
                'scope': 'calendar:calendar:read calendar:calendar.event:read offline_access'}

    def subject(self, token):
        if isinstance(token, str) and token.startswith('stub-'):
            return token[len('stub-'):]
        raise ValueError('stub subject decode failed')

    def user_info(self, token):
        open_id = self.subject(token)
        return {'open_id': open_id, 'name': f'用户 {open_id}',
                'en_name': f'Stub {open_id}', 'email': f'{open_id}@stub.local'}
