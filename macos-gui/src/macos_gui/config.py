"""Device-local configuration: server_url + device_token.

The device_token is a *device credential* (CONTRACT_ADVISOR_API §8), not a
user OAuth token; the red line "user credentials never leave the cloud" is
unchanged. Storage goes through the OS keychain (macOS Keychain / Windows
Credential Manager via keyring); nothing is written to plain files.
"""

SERVICE = 'macos-gui'
KEY_SERVER_URL = 'server_url'
KEY_DEVICE_TOKEN = 'device_token'


class MemoryConfigStore:
    """In-memory store (tests and keyring-less environments)."""

    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class KeyringConfigStore:
    """OS keychain-backed store; falls back to memory when no backend exists."""

    def __init__(self):
        self._fallback = MemoryConfigStore()
        self._use_keyring = True

    def _call(self, operation, key, *args):
        if not self._use_keyring:
            return getattr(self._fallback, operation)(key, *args)
        import keyring
        try:
            return getattr(keyring, operation)(SERVICE, key, *args)
        except Exception:
            self._use_keyring = False
            return getattr(self._fallback, operation)(key, *args)

    def get(self, key):
        return self._call('get_password', key)

    def set(self, key, value):
        self._call('set_password', key, value)

    def delete(self, key):
        self._call('delete_password', key)


def default_store():
    return KeyringConfigStore()


def normalize_server_url(url):
    """Strip trailing slashes; keep scheme+host as the API base URL."""
    if not isinstance(url, str):
        raise ValueError('server url must be a string')
    value = url.strip().rstrip('/')
    if not value.startswith(('http://', 'https://')):
        raise ValueError('server url must start with http:// or https://')
    if len(value) > 512:
        raise ValueError('server url too long')
    return value


def load_state(store):
    return {'server_url': store.get(KEY_SERVER_URL) or None,
            'device_token': store.get(KEY_DEVICE_TOKEN) or None}


def save_server_url(store, url):
    value = normalize_server_url(url)
    store.set(KEY_SERVER_URL, value)
    return value


def save_device_token(store, token):
    if not isinstance(token, str) or not token:
        raise ValueError('device token required')
    store.set(KEY_DEVICE_TOKEN, token)


def clear_device_token(store):
    store.delete(KEY_DEVICE_TOKEN)
