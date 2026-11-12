"""Device-flow login state machine (framework-free; the Qt layer drives ticks).

States: begin() -> awaiting_scan; tick() returns one of
  awaiting_scan | completed (terminal, carries device_token+user)
  | expired (terminal: regenerate the QR) | unavailable (transient: keep ticking).
"""
from .api import ApiError, NetworkError

TRANSIENT_CODES = ('auth_unavailable',)
EXPIRED_CODES = ('expired', 'unknown_device')


class LoginFlow:
    def __init__(self, client):
        self.client = client
        self._device_code = None

    @property
    def in_progress(self):
        return self._device_code is not None

    def begin(self):
        """Request a fresh QR login; returns {login_url, device_code, expires_at}."""
        result = self.client.login_start()
        device_code = result.get('device_code')
        if not isinstance(device_code, str) or not device_code:
            raise NetworkError('login/start reply missing device_code')
        self._device_code = device_code
        return result

    def tick(self):
        """Advance the login; returns an event dict (see module docstring)."""
        if self._device_code is None:
            raise RuntimeError('call begin() before tick()')
        try:
            result = self.client.login_poll(self._device_code)
        except NetworkError:
            return {'state': 'unavailable'}
        except ApiError as exc:
            if exc.code in EXPIRED_CODES:
                self._device_code = None
                return {'state': 'expired'}
            if exc.code in TRANSIENT_CODES:
                return {'state': 'unavailable'}
            raise
        if result.get('status') == 'completed':
            self._device_code = None
            token = result.get('device_token')
            if not isinstance(token, str) or not token:
                return {'state': 'expired'}
            return {'state': 'completed', 'device_token': token,
                    'user': result.get('user') or {}}
        return {'state': 'awaiting_scan'}
