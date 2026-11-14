"""Meeting catalog isolation, concurrent prewarm and recovery."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from llm_gateway.catalog import MeetingCatalogs


class Query:
    def __init__(self):
        self.calls = []
        self.gate = threading.Event()

    def info(self, owner):
        self.calls.append(owner)
        self.gate.wait(3)
        return {'tenants': ['NIO'], 'platforms': [{'versions': [owner]}]}


class MeetingCatalogTests(unittest.TestCase):
    def test_isolation_single_flight_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'catalog.sqlite3'
            query = Query()
            catalogs = MeetingCatalogs(path, query)
            catalogs.prepare('s-a', 'ou_A')
            catalogs.prepare('s-a', 'ou_A')
            self.assertIsNone(catalogs.get('s-a', 'ou_A'))
            with self.assertRaises(ValueError):
                catalogs.prepare('s-a', 'ou_B')
            query.gate.set()
            end = time.monotonic() + 3
            while catalogs.get('s-a', 'ou_A') is None and time.monotonic() < end:
                time.sleep(0.01)
            self.assertEqual(query.calls, ['ou_A'])
            self.assertIsNone(catalogs.get('s-a', 'ou_B'))
            first = catalogs.get('s-a', 'ou_A')
            catalogs.close()
            catalogs = MeetingCatalogs(path, query)
            catalogs.prepare('s-a', 'ou_A')
            self.assertEqual(catalogs.get('s-a', 'ou_A'), first)
            catalogs.prepare('s-b', 'ou_A')
            end = time.monotonic() + 3
            while catalogs.get('s-b', 'ou_A') is None and time.monotonic() < end:
                time.sleep(0.01)
            catalogs.close()
            self.assertEqual(query.calls, ['ou_A', 'ou_A'])


if __name__ == '__main__':
    unittest.main()
