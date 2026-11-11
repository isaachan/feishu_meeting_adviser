"""Advisor API client per CONTRACT_ADVISOR_API v1 (bot side)."""
import json
from uuid import NAMESPACE_URL, uuid5

from urllib import error, request


class AdvisorApiError(Exception):
    def __init__(self, status, code, message, extra=None):
        self.status, self.code, self.message = status, code, message
        self.extra = extra or {}
        super().__init__(f'{status} {code}')


class AdvisorClient:
    def __init__(self, base_url='http://127.0.0.1:8380', *, timeout=10, opener=None):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = opener or request.build_opener(request.ProxyHandler({})).open

    def _call(self, method, path, body=None, *, idempotency_key=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if idempotency_key:
            headers['X-Idempotency-Key'] = idempotency_key
        req = request.Request(self.base_url + path, data=data, method=method,
                              headers=headers)
        try:
            with self.opener(req, timeout=self.timeout) as response:
                raw = response.read()
            return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            payload = {}
            try:
                payload = json.loads(exc.read() or b'{}')
            except ValueError:
                pass
            details = payload.get('error', {})
            raise AdvisorApiError(exc.code, details.get('code', 'internal'),
                                  details.get('message', ''), details) from None
        except (error.URLError, OSError, ValueError) as exc:
            raise AdvisorApiError(503, 'advisor_unreachable', str(exc.__class__.__name__)) from exc

    @staticmethod
    def key_for(message_id: str, action: str) -> str:
        return str(uuid5(NAMESPACE_URL, f'{message_id}:{action}'))

    # -- sessions ------------------------------------------------------------

    def create_session(self, owner_open_id, meeting_no, *, password='', mic=False,
                       message_id='local'):
        return self._call('POST', '/sessions',
                          {'owner_open_id': owner_open_id,
                           'meeting_ref': {'no': meeting_no, 'password': password},
                           'mic_enabled': mic},
                          idempotency_key=self.key_for(message_id, 'create-session'))

    def list_meetings(self, owner_open_id):
        return self._call('GET', f'/meetings?owner_open_id={owner_open_id}')

    def list_sessions(self, owner_open_id):
        return self._call('GET', f'/sessions?owner_open_id={owner_open_id}')

    def session_status(self, session_id):
        return self._call('GET', f'/sessions/{session_id}/status')

    def end_session(self, session_id, owner_open_id, *, message_id='local'):
        return self._call('POST', f'/sessions/{session_id}/end',
                          {'owner_open_id': owner_open_id},
                          idempotency_key=self.key_for(message_id, 'end-session'))

    # -- subscriptions ---------------------------------------------------------

    def create_subscription(self, subscriber_open_id, meeting_no, *, message_id='local'):
        return self._call('POST', '/subscription-requests',
                          {'subscriber_open_id': subscriber_open_id,
                           'meeting_no': meeting_no},
                          idempotency_key=self.key_for(message_id, 'subscribe'))

    def decide_subscription(self, request_no, owner_open_id, decision, *, message_id='local'):
        return self._call('POST', f'/subscription-requests/{request_no}/decide',
                          {'owner_open_id': owner_open_id, 'decision': decision},
                          idempotency_key=self.key_for(message_id, f'decide-{request_no}'))

    def revoke_subscription(self, request_no, owner_open_id, *, message_id='local'):
        return self._call('POST', f'/subscriptions/{request_no}/revoke',
                          {'owner_open_id': owner_open_id},
                          idempotency_key=self.key_for(message_id, f'revoke-{request_no}'))

    def revoke_subscription_by_subscriber(self, request_no, subscriber_open_id, *,
                                          message_id='local'):
        return self._call('POST', f'/subscriptions/{request_no}/revoke',
                          {'subscriber_open_id': subscriber_open_id},
                          idempotency_key=self.key_for(message_id, f'revoke-{request_no}'))

    def my_subscriptions(self, subscriber_open_id):
        return self._call('GET', f'/subscriptions?subscriber_open_id={subscriber_open_id}')

    # -- outbox ----------------------------------------------------------------

    def pending_outbox(self, limit=10):
        return self._call('GET', f'/outbox/pending?limit={limit}')

    def outbox_result(self, uuid, status, *, error='', message_id='local'):
        return self._call('POST', f'/outbox/{uuid}/result',
                          {'status': status, 'error': error},
                          idempotency_key=self.key_for(message_id, f'outbox-{uuid}'))
