"""Device-local configuration: server_url + device_token.

The device_token is a *device credential* (CONTRACT_ADVISOR_API §8), not a
user OAuth token; the red line "user credentials never leave the cloud" is
unchanged. Storage goes through the OS keychain (macOS Keychain / Windows
Credential Manager via keyring); nothing is written to plain files.
"""

SERVICE = 'gui'
LEGACY_SERVICE = 'macos-gui'  # 2026-11-23 模块更名前的服务名；仅用于一次性迁移读取
KEY_SERVER_URL = 'server_url'
KEY_DEVICE_TOKEN = 'device_token'
KEY_AUTO_CONNECT = 'auto_connect'
KEY_TRAY_HINT_SHOWN = 'tray_hint_shown'

# 首开免配置：未显式保存过地址时使用的默认会参谋服务器（docs/DEPLOYMENT.md）。
DEFAULT_SERVER_URL = 'http://meeting-advisor.nioint.com'


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

    # keyring operation -> MemoryConfigStore method (APIs differ).
    _FALLBACK_OPS = {'get_password': 'get', 'set_password': 'set',
                     'delete_password': 'delete'}

    def __init__(self):
        self._fallback = MemoryConfigStore()
        self._use_keyring = True

    def _call(self, operation, key, *args):
        if not self._use_keyring:
            return getattr(self._fallback, self._FALLBACK_OPS[operation])(key, *args)
        import keyring
        try:
            return getattr(keyring, operation)(SERVICE, key, *args)
        except Exception:
            self._use_keyring = False
            return getattr(self._fallback, self._FALLBACK_OPS[operation])(key, *args)

    def get(self, key):
        return self._call('get_password', key)

    def set(self, key, value):
        self._call('set_password', key, value)

    def delete(self, key):
        self._call('delete_password', key)


def default_store():
    store = KeyringConfigStore()
    migrate_legacy_service(store)
    return store


def migrate_legacy_service(store):
    """把旧服务名（macos-gui）下保存的配置一次性拷贝到新服务名（gui）。

    Best-effort：任何失败（无 keyring 后端、Keychain ACL 拒绝）都只是
    保持新服务名为空——用户回到首次使用流程重新填写，行为不变。
    """
    if not isinstance(store, KeyringConfigStore):
        return
    try:
        import keyring
        for key in (KEY_SERVER_URL, KEY_DEVICE_TOKEN, KEY_AUTO_CONNECT):
            if keyring.get_password(SERVICE, key) is not None:
                continue
            value = keyring.get_password(LEGACY_SERVICE, key)
            if value is not None:
                keyring.set_password(SERVICE, key, value)
    except Exception:
        pass


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
            'device_token': store.get(KEY_DEVICE_TOKEN) or None,
            'auto_connect': store.get(KEY_AUTO_CONNECT) == 'true'}


def save_auto_connect(store, enabled):
    store.set(KEY_AUTO_CONNECT, 'true' if enabled else 'false')


def tray_hint_shown(store):
    return store.get(KEY_TRAY_HINT_SHOWN) == 'true'


def mark_tray_hint_shown(store):
    store.set(KEY_TRAY_HINT_SHOWN, 'true')


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
