"""Inference task identity includes the trusted session owner."""
import unittest

from advisor.model_backend import remote_identity
from advisor.settings import AdviserSettings


class RemoteIdentityTests(unittest.TestCase):
    def test_owner_separates_identical_snapshots(self):
        settings = AdviserSettings()
        first = remote_identity('role', {'generation': 1}, settings, 'ou_A')
        self.assertEqual(first, remote_identity('role', {'generation': 1}, settings, 'ou_A'))
        self.assertNotEqual(first, remote_identity('role', {'generation': 1}, settings, 'ou_B'))
        self.assertNotEqual(first, remote_identity('role', {'generation': 1}, settings))


if __name__ == '__main__':
    unittest.main()
