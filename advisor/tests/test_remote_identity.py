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
        meeting_a = remote_identity('role', {'generation': 1}, settings, 'ou_A', 's-a')
        meeting_b = remote_identity('role', {'generation': 1}, settings, 'ou_A', 's-b')
        self.assertNotEqual(meeting_a, meeting_b)

    def test_decision_output_version_changes_identity(self):
        v1 = remote_identity('role', {'generation': 1},
                             AdviserSettings(decision_output='decision-v1'), 'ou_A')
        v2 = remote_identity('role', {'generation': 1},
                             AdviserSettings(decision_output='decision-v2'), 'ou_A')
        self.assertNotEqual(v1, v2)
        self.assertEqual(remote_identity('role', {'generation': 1},
                                         AdviserSettings(), 'ou_A'), v2)

    def test_owner_is_mandatory(self):
        settings = AdviserSettings()
        with self.assertRaises(TypeError):
            remote_identity('role', {'generation': 1}, settings)


if __name__ == '__main__':
    unittest.main()
