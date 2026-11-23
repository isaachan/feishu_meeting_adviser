import sys
import unittest
from unittest import mock

from gui import config


class _BrokenKeyring:
    """Simulates keyring failures (e.g. packaged app rejected by Keychain ACL)."""

    def get_password(self, service, key):
        raise RuntimeError('keychain refused')

    def set_password(self, service, key, value):
        raise RuntimeError('keychain refused')

    def delete_password(self, service, key):
        raise RuntimeError('keychain refused')


class MemoryStoreTests(unittest.TestCase):
    def test_roundtrip_and_clear(self):
        store = config.MemoryConfigStore()
        self.assertEqual(config.load_state(store),
                         {'server_url': None, 'device_token': None,
                          'auto_connect': False})
        config.save_auto_connect(store, True)
        self.assertTrue(config.load_state(store)['auto_connect'])
        config.save_auto_connect(store, False)
        self.assertFalse(config.load_state(store)['auto_connect'])
        config.save_server_url(store, 'https://advisor.internal/')
        self.assertEqual(config.load_state(store)['server_url'],
                         'https://advisor.internal')
        config.save_device_token(store, 'dt-abc')
        self.assertEqual(config.load_state(store)['device_token'], 'dt-abc')
        config.clear_device_token(store)
        self.assertIsNone(config.load_state(store)['device_token'])

    def test_server_url_validation(self):
        store = config.MemoryConfigStore()
        for bad in ('', 'advisor.internal', 'ftp://x', None, 7, 'x' * 600):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.save_server_url(store, bad)

    def test_default_server_url_is_valid(self):
        self.assertEqual(config.normalize_server_url(config.DEFAULT_SERVER_URL),
                         config.DEFAULT_SERVER_URL)

    def test_device_token_validation(self):
        store = config.MemoryConfigStore()
        for bad in ('', None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.save_device_token(store, bad)

    def test_keyring_store_falls_back_to_memory(self):
        store = config.KeyringConfigStore()
        store.set('server_url', 'https://x')
        self.assertEqual(store.get('server_url'), 'https://x')
        store.delete('server_url')
        self.assertIsNone(store.get('server_url'))

    def test_keyring_failure_falls_back_without_crash(self):
        store = config.KeyringConfigStore()
        with mock.patch.dict(sys.modules, {'keyring': _BrokenKeyring()}):
            self.assertIsNone(store.get('server_url'))  # used to AttributeError
            store.set('device_token', 'dt-abc')
            self.assertEqual(store.get('device_token'), 'dt-abc')
            store.delete('device_token')
            self.assertIsNone(store.get('device_token'))
        # fallback is sticky after the first failure
        store.set('server_url', 'https://y')
        self.assertEqual(store.get('server_url'), 'https://y')


if __name__ == '__main__':
    unittest.main()
