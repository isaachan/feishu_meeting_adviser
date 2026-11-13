"""HTTP client for the auth node (CONTRACT_AUTH internal API, advisor side)."""
import json
from urllib import error, parse, request


class AuthorizationRequired(Exception):
    def __init__(self, url, provider='feishu'):
        self.url = url
        self.provider = provider
        self.authorize_url = url  # legacy attribute name (feishu callers)
        super().__init__(f'authorization_required:{provider}')


class AuthUnavailable(Exception):
    pass


class HttpAuthClient:
    def __init__(self, base_url='http://127.0.0.1:8381'):
        self.base_url = base_url.rstrip('/')

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(self.base_url + path, data=data, method=method,
                              headers={'Content-Type': 'application/json'})
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(req, timeout=10) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    @staticmethod
    def _error_code(exc):
        payload = {}
        try:
            payload = json.loads(exc.read() or b'{}')
        except ValueError:
            pass
        return payload.get('error', {}).get('code')

    def get_token(self, open_id, *, rejected_token=None):
        """Returns {'access_token', 'expires_at', 'scopes'} (Feishu provider).

        Raises AuthorizationRequired (with authorize_url) or AuthUnavailable.
        """
        query = f'?rejected={rejected_token}' if rejected_token else ''
        try:
            return self._call('GET', f'/credentials/{open_id}/token{query}')
        except error.HTTPError as exc:
            code = self._error_code(exc)
            if code == 'reauthorization_required' or exc.code == 404:
                # not_authorized / reauthorization_required both mean: send a link
                raise AuthorizationRequired(self.authorize_url(open_id)) from None
            if exc.code in (503, 400):
                raise AuthUnavailable(code or exc.code) from None
            raise AuthUnavailable('internal') from None
        except (error.URLError, OSError, ValueError) as exc:
            raise AuthUnavailable('network') from exc

    def authorize_url(self, open_id, intent=''):
        result = self._call('POST', '/authorize/start',
                            {'open_id': open_id, 'intent': intent})
        return result['authorize_url']

    # -- NSDP provider (CONTRACT_AUTH §8) -------------------------------------

    def nsdp_submit_url(self, open_id):
        result = self._call('POST', '/token-submissions/start',
                            {'open_id': open_id, 'provider': 'nsdp'})
        return result['submit_url']

    def get_nsdp_token(self, open_id):
        """Returns {'token', 'updated_at'} or raises AuthorizationRequired
        (url = one-shot submit link) / AuthUnavailable."""
        try:
            return self._call('GET', f'/credentials/{open_id}/token?provider=nsdp')
        except error.HTTPError as exc:
            code = self._error_code(exc)
            if code == 'not_authorized' or exc.code == 404:
                try:
                    url = self.nsdp_submit_url(open_id)
                except (error.HTTPError, error.URLError, OSError, ValueError):
                    raise AuthUnavailable('submit_start') from None
                raise AuthorizationRequired(url, provider='nsdp') from None
            if exc.code in (503, 400):
                raise AuthUnavailable(code or exc.code) from None
            raise AuthUnavailable('internal') from None
        except (error.URLError, OSError, ValueError) as exc:
            raise AuthUnavailable('network') from exc

    def invalidate_nsdp(self, open_id):
        """Drop the stored NSDP token (advisor observed 401 from NSDP)."""
        try:
            self._call('POST', f'/credentials/{open_id}/invalidate',
                       {'provider': 'nsdp'})
        except (error.HTTPError, error.URLError, OSError, ValueError):
            pass  # best effort: a stale record still reads as unauthorized later

    # -- GUI discovery login (CONTRACT_AUTH v1.2) ------------------------------

    def user_profile(self, open_id):
        """Stored identity summary (CONTRACT_AUTH v1.3 §2.4).

        Returns {'open_id', 'name', 'en_name', 'email'} or None when the user
        never authorized; raises AuthUnavailable on transport/server failure.
        """
        try:
            return self._call('GET', f'/users/{open_id}/profile')
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise AuthUnavailable('profile') from None
        except (error.URLError, OSError, ValueError) as exc:
            raise AuthUnavailable('network') from exc

    def gui_authorize_start(self):
        """Returns {'authorize_url', 'state', 'expires_at'} for the QR login."""
        try:
            return self._call('POST', '/authorize/start',
                              {'open_id': None, 'intent': 'gui:login'})
        except (error.HTTPError, error.URLError, OSError, ValueError) as exc:
            raise AuthUnavailable('gui_start') from exc

    def authorize_status(self, state):
        """Returns {'status': 'pending'|'completed'|'expired', 'user'?: {...}}.

        Auth maps unknown/expired states to 404; the GUI face surfaces that as
        'expired' so the client regenerates the QR.
        """
        try:
            return self._call('GET', '/authorize/status?state=' + parse.quote(state))
        except error.HTTPError as exc:
            if exc.code == 404:
                return {'status': 'expired'}
            raise AuthUnavailable('status') from None
        except (error.URLError, OSError, ValueError) as exc:
            raise AuthUnavailable('network') from exc
