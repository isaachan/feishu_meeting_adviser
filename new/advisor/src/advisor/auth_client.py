"""HTTP client for the auth node (CONTRACT_AUTH internal API, advisor side)."""
import json
from urllib import error, request


class AuthorizationRequired(Exception):
    def __init__(self, authorize_url):
        self.authorize_url = authorize_url
        super().__init__('authorization_required')


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

    def get_token(self, open_id, *, rejected_token=None):
        """Returns {'access_token', 'expires_at', 'scopes'}.

        Raises AuthorizationRequired (with authorize_url) or AuthUnavailable.
        """
        query = f'?rejected={rejected_token}' if rejected_token else ''
        try:
            return self._call('GET', f'/credentials/{open_id}/token{query}')
        except error.HTTPError as exc:
            payload = {}
            try:
                payload = json.loads(exc.read() or b'{}')
            except ValueError:
                pass
            code = payload.get('error', {}).get('code')
            if code == 'reauthorization_required':
                raise AuthorizationRequired(self.authorize_url(open_id)) from None
            if exc.code in (404, 503, 400):
                raise AuthUnavailable(code or exc.code) from None
            raise AuthUnavailable('internal') from None
        except (error.URLError, OSError, ValueError) as exc:
            raise AuthUnavailable('network') from exc

    def authorize_url(self, open_id, intent=''):
        result = self._call('POST', '/authorize/start',
                            {'open_id': open_id, 'intent': intent})
        return result['authorize_url']
