"""Owner-scoped release cache across independent MCP processes."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from llm_gateway.nsdp_cache import OwnerReleaseCache
from llm_gateway.nsdp_query import QueryError


class OwnerReleaseCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'catalog.sqlite3'
        self.now = 1000.0

    def cache(self):
        cache = OwnerReleaseCache(self.path, clock=lambda: self.now)
        self.addCleanup(cache.close)
        return cache

    def test_owner_release_ttl_and_restart(self):
        first = self.cache()
        calls = []

        def fetch():
            calls.append(True)
            return 'release data'

        self.assertEqual(first.lookup('ou_A', 'BL1.5.0', 'token-A', fetch)[1], False)
        second = self.cache()  # a fresh MCP process sees the same persisted result
        self.assertEqual(second.lookup('ou_A', 'bl1.5.0', 'token-A', fetch)[:3],
                         ('release data', True, 0))
        second.lookup('ou_A', 'BL1.6.0', 'token-A', fetch)
        second.lookup('ou_B', 'BL1.5.0', 'token-B', fetch)
        self.assertEqual(len(calls), 3)
        self.now += 3599
        self.assertTrue(second.lookup('ou_A', 'BL1.5.0', 'token-A', fetch)[1])
        self.now += 1
        self.assertFalse(second.lookup('ou_A', 'BL1.5.0', 'token-A', fetch)[1])
        self.assertEqual(len(calls), 4)

    def test_credential_change_and_invalidate(self):
        cache = self.cache()
        calls = []

        def fetch():
            calls.append(True)
            return 'data'

        cache.lookup('ou_A', 'BL1.5.0', 'old-token', fetch)
        self.assertFalse(cache.lookup('ou_A', 'BL1.5.0', 'new-token', fetch)[1])
        self.assertEqual(len(calls), 2)
        self.assertNotIn(b'old-token', self.path.read_bytes())
        cache.invalidate('ou_A')
        self.assertFalse(cache.lookup('ou_A', 'BL1.5.0', 'new-token', fetch)[1])
        self.assertEqual(len(calls), 3)

    def test_concurrent_same_key_runs_fetch_once(self):
        first, second = self.cache(), self.cache()
        entered, release = threading.Event(), threading.Event()
        calls = []
        results = []

        def fetch():
            calls.append(True)
            entered.set()
            release.wait(2)
            return 'shared'

        def lookup(cache):
            results.append(cache.lookup('ou_A', 'BL1.5.0', 'token', fetch))

        one = threading.Thread(target=lookup, args=(first,))
        two = threading.Thread(target=lookup, args=(second,))
        one.start()
        self.assertTrue(entered.wait(2))
        two.start()
        time.sleep(0.1)
        release.set()
        one.join(2)
        two.join(2)
        self.assertFalse(one.is_alive() or two.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(result[1] for result in results), [False, True])

    def test_failure_is_not_cached_and_releases_lock(self):
        cache = self.cache()
        with self.assertRaises(QueryError):
            cache.lookup('ou_A', 'BL1.5.0', 'token',
                         lambda: (_ for _ in ()).throw(QueryError('timeout')))
        self.assertFalse(cache.lookup('ou_A', 'BL1.5.0', 'token',
                                      lambda: 'recovered')[1])

    def test_expiry_sweep_does_not_discard_an_inflight_first_result(self):
        cache = self.cache()

        def fetch():
            cache.expire()
            return 'completed after sweep'

        self.assertFalse(cache.lookup('ou_A', 'BL1.5.0', 'token', fetch)[1])
        self.assertTrue(cache.lookup('ou_A', 'BL1.5.0', 'token',
                                     lambda: self.fail('unexpected refetch'))[1])

    def test_lost_hmac_key_discards_persisted_results(self):
        first = self.cache()
        first.lookup('ou_A', 'BL1.5.0', 'token', lambda: 'old data')
        self.path.with_name(self.path.name + '.hmac').unlink()
        second = self.cache()
        self.assertFalse(second.lookup('ou_A', 'BL1.5.0', 'token',
                                       lambda: 'fresh data')[1])


if __name__ == '__main__':
    unittest.main()
