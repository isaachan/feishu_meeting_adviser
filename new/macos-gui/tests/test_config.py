import unittest

from macos_gui import config


class MemoryStoreTests(unittest.TestCase):
    def test_roundtrip_and_clear(self):
        store = config.MemoryConfigStore()
        self.assertEqual(config.load_state(store),
                         {'server_url': None, 'device_token': None})
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


if __name__ == '__main__':
    unittest.main()
