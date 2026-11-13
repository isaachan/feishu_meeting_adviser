"""Auth service core: authorize URL, callback exchange, single-flight refresh.

Ported from legacy oauth.py (OAuthManager.access_token / authorization_parameters
/ callback_code), adapted to cloud multi-user: records keyed by open_id, state
table persisted locally, transport injectable for tests.
"""

import base64
import hashlib
import re
import secrets
import threading
import time
from pathlib import Path
from urllib import parse

from .constants import (AUTHORIZE_URL, CALLBACK_PATH, DEFAULT_STATE_TTL_S, GUI_LOGIN_INTENT,
                        NSDP_INTENT, NSDP_SUBMIT_PATH, SCOPES)
from .errors import AuthError
from .state_table import StateTable
from .storage import digest
from .store import CredentialStore, NsdpCredentialStore, valid_open_id
from .transport import FeishuTransport

_CODE = re.compile(r'[A-Za-z0-9_-]{1,4096}\Z')


class AuthService:
    def __init__(self, credentials, data_dir, *, base_url, transport=None,
                 clock=time.time, state_ttl_s=DEFAULT_STATE_TTL_S):
        self.credentials = credentials
        self.clock = clock
        self.base_url = base_url.rstrip('/')
        parsed = parse.urlsplit(self.base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment:
            raise AuthError('oauth_invalid_callback')
        self.redirect_uri = self.base_url + CALLBACK_PATH
        self.transport = transport or FeishuTransport(credentials)
        data = Path(data_dir)
        self.store = CredentialStore(data / 'credentials')
        self.nsdp_store = NsdpCredentialStore(data / 'credentials' / 'nsdp')
        self.states = StateTable(data / 'pending_states.json', clock, ttl_s=state_ttl_s)
        self._flight_guard = threading.Lock()
        self._flights = {}

    def _flight(self, open_id):
        with self._flight_guard:
            lock = self._flights.get(open_id)
            if lock is None:
                lock = threading.Lock()
                self._flights[open_id] = lock
            return lock

    def authorize_start(self, open_id, intent):
        """Issue an authorize URL. Discovery variant (v1.2): intent=gui:login
        takes open_id=None; the subject is discovered at callback time."""
        discovery = intent == GUI_LOGIN_INTENT
        if discovery and open_id is not None:
            raise AuthError('oauth_invalid_open_id')
        if (not discovery and not valid_open_id(open_id)) or \
                not isinstance(intent, str) or len(intent) > 128:
            raise AuthError('oauth_invalid_open_id')
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
        entry = self.states.issue(open_id, intent, self.redirect_uri, verifier,
                                  discovery=discovery)
        url = AUTHORIZE_URL + '?' + parse.urlencode({
            'client_id': self.credentials.app_id, 'response_type': 'code',
            'redirect_uri': self.redirect_uri, 'scope': ' '.join(SCOPES),
            'state': entry['state'], 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'prompt': 'consent'})
        return {'authorize_url': url, 'state': entry['state'],
                'redirect_uri': self.redirect_uri, 'expires_at': entry['expires_at']}

    def callback(self, query_string):
        """Process the OAuth callback query; returns a render-ready outcome."""
        params = self._callback_params(query_string)
        state = params['state']
        if params['error'] is not None:
            if params['error'] == 'access_denied' and params['code'] is None:
                self.states.release(state)
                return {'outcome': 'denied'}
            return {'outcome': 'invalid'}
        if params['code'] is None:
            return {'outcome': 'invalid'}
        try:
            entry = self.states.take(state)
        except AuthError:
            return {'outcome': 'invalid'}
        started = self.clock()
        try:
            result = self.transport.exchange(params['code'], entry['redirect_uri'], entry['verifier'])
            info = self.transport.user_info(result['access_token'])
            if entry.get('discovery') or entry['open_id'] is None:
                open_id = info['open_id']  # discovery: adopt the scanned identity
            else:
                open_id = entry['open_id']
                if info['open_id'] != open_id:
                    return {'outcome': 'failed', 'stage': 'identity', 'open_id': open_id}
            record = self.store.token_record(result, started=started, open_id=open_id,
                                             subject_hash=None)
            record['subject_hash'] = digest([self.credentials.app_id, info['open_id']])
            with self.store.locked(open_id):
                self.store.write(open_id, record)
            self.states.complete(state, info)
        except AuthError as exc:
            return {'outcome': 'failed', 'stage': exc.stage or 'validation', 'open_id': entry['open_id']}
        return {'outcome': 'success', 'open_id': open_id, 'intent': entry['intent']}

    def authorize_status(self, state):
        """Poll a login state (CONTRACT_AUTH v1.2 §2.3): pending/completed.

        Never returns tokens; completed carries only the identity summary
        persisted by the callback.
        """
        entry = self.states.lookup(state)
        if entry is None:
            raise AuthError('oauth_unknown_state')
        user = entry.get('user')
        if isinstance(user, dict) and valid_open_id(user.get('open_id')):
            return {'status': 'completed', 'user': {
                'open_id': user['open_id'], 'name': user.get('name'),
                'en_name': user.get('en_name'), 'email': user.get('email')}}
        return {'status': 'pending'}

    def _callback_params(self, query_string):
        if not isinstance(query_string, str) or len(query_string) > 8192:
            raise AuthError('oauth_invalid_callback')
        try:
            params = parse.parse_qs(query_string, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            raise AuthError('oauth_invalid_callback') from None
        states = params.get('state', [])
        if len(states) != 1 or not states[0]:
            raise AuthError('oauth_invalid_callback')
        errors = params.get('error', [])
        if len(errors) > 1:
            raise AuthError('oauth_invalid_callback')
        error = errors[0] if errors else None
        codes = params.get('code', [])
        code = None
        if codes:
            if len(codes) != 1 or not _CODE.fullmatch(codes[0]):
                raise AuthError('oauth_invalid_callback')
            code = codes[0]
        return {'state': states[0], 'error': error, 'code': code}

    def get_valid_token(self, open_id, *, rejected_token=None):
        """Return a usable access token, silently refreshing when near expiry.

        Single-flight per open_id: concurrent callers share one refresh POST.
        Refresh semantics ported from legacy access_token(): marker committed
        before the one-use POST; refresh token must rotate; any failure leaves
        reauthorization_required (fail-closed, never replay).
        """
        if not valid_open_id(open_id):
            raise AuthError('oauth_invalid_open_id')
        with self._flight(open_id), self.store.locked(open_id):
            value = self.store.read(open_id)
            if value['state'] != 'ready':
                raise AuthError('oauth_reauthorization_required')
            self.store.validated_ready(value)
            reusable = self.clock() < value['expires_at'] - value['refresh_margin']
            if reusable and (rejected_token is None or value['access_token'] != rejected_token):
                return self._token_response(value)
            if self.clock() >= value['refresh_expires_at']:
                self.store.write(open_id, self.store.marker(
                    open_id, 'reauthorization_required', value.get('subject_hash')))
                raise AuthError('oauth_reauthorization_required')
            # Commit before the one-use POST. A crash at any later point forbids reuse.
            self.store.write(open_id, self.store.marker(
                open_id, 'refreshing', value.get('subject_hash')))
            started = self.clock()
            try:
                result = self.transport.refresh(value['refresh_token'])
                replacement = self.store.token_record(
                    result, started=started, open_id=open_id,
                    subject_hash=value.get('subject_hash'))
                if replacement['refresh_token'] == value['refresh_token']:
                    raise AuthError('oauth_invalid_response')
                self.store.write(open_id, replacement)
            except (AuthError, OSError):
                self.store.write(open_id, self.store.marker(
                    open_id, 'reauthorization_required', value.get('subject_hash')))
                raise
            return self._token_response(replacement)

    @staticmethod
    def _token_response(value):
        return {'access_token': value['access_token'], 'expires_at': value['expires_at'],
                'scopes': list(value['scopes'])}

    # -- NSDP token submissions (CONTRACT_AUTH §8) -----------------------------

    def nsdp_submission_start(self, open_id):
        """One-shot submit link bound to the open_id (same state table/TTL)."""
        if not valid_open_id(open_id):
            raise AuthError('oauth_invalid_open_id')
        entry = self.states.issue(open_id, NSDP_INTENT,
                                  self.base_url + NSDP_SUBMIT_PATH, '')
        return {'submit_url': f"{entry['redirect_uri']}?state={entry['state']}",
                'state': entry['state'], 'expires_at': entry['expires_at']}

    def nsdp_peek(self, state):
        entry = self.states.peek(state)
        if entry.get('intent') != NSDP_INTENT:
            raise AuthError('oauth_invalid_callback')
        return entry

    def nsdp_submit(self, state, token):
        """Consume the state (one-shot) and store the token for its open_id."""
        entry = self.states.take(state)
        if entry.get('intent') != NSDP_INTENT:
            raise AuthError('oauth_invalid_callback')
        open_id = entry['open_id']
        if not isinstance(token, str) or not token.strip():
            raise AuthError('nsdp_invalid_token')
        token = token.strip()
        with self.nsdp_store.locked(open_id):
            self.nsdp_store.write(open_id, token, now=self.clock())
        return {'open_id': open_id}

    def get_nsdp_token(self, open_id):
        if not valid_open_id(open_id):
            raise AuthError('oauth_invalid_open_id')
        record = self.nsdp_store.read(open_id)
        if record is None:
            raise AuthError('oauth_not_authorized')
        return {'token': record['token'], 'updated_at': record['updated_at']}

    def invalidate_nsdp(self, open_id):
        if not valid_open_id(open_id):
            raise AuthError('oauth_invalid_open_id')
        with self.nsdp_store.locked(open_id):
            self.nsdp_store.delete(open_id)
        return {'ok': True}
