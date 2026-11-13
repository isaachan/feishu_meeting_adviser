"""LogFollower acceptance: bootstrap bounding, appends, rotation, truncation."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from admin_site.logtail import ROTATED_MARKER, TRUNCATED_MARKER, LogFollower


class LogTailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'advisor.log'

    def write(self, data, truncate=False):
        payload = data.encode('utf-8') if isinstance(data, str) else data
        with open(self.path, 'wb' if truncate else 'ab') as fh:
            fh.write(payload)

    def test_missing_file_yields_nothing_then_bootstraps_on_creation(self):
        follower = LogFollower(self.path)
        self.assertEqual(follower.read_new(), '')
        self.write('hello\n')
        self.assertEqual(follower.read_new(), 'hello\n')
        self.addCleanup(follower.close)

    def test_bootstrap_bounded_with_marker(self):
        self.write('x' * 3000)
        follower = LogFollower(self.path, bootstrap_bytes=1000)
        text = follower.read_new()
        self.assertTrue(text.startswith(TRUNCATED_MARKER + '\n'))
        self.assertEqual(len(text), len(TRUNCATED_MARKER) + 1 + 1000)
        self.assertTrue(text.endswith('x' * 1000))
        self.addCleanup(follower.close)

    def test_small_file_bootstraps_whole(self):
        self.write('line1\nline2\n')
        follower = LogFollower(self.path)
        self.assertEqual(follower.read_new(), 'line1\nline2\n')
        self.assertEqual(follower.read_new(), '')  # nothing new
        self.write('line3\n')
        self.assertEqual(follower.read_new(), 'line3\n')
        self.addCleanup(follower.close)

    def test_in_place_truncation_restarts(self):
        self.write('old-a\nold-b\n')
        follower = LogFollower(self.path)
        self.assertEqual(follower.read_new(), 'old-a\nold-b\n')
        self.write('new-a\n', truncate=True)  # rewrite: file shrinks
        text = follower.read_new()
        self.assertTrue(text.startswith(ROTATED_MARKER + '\n'))
        self.assertIn('new-a\n', text)
        self.assertNotIn('old-a', text)
        self.addCleanup(follower.close)

    def test_rotation_to_new_inode_restarts(self):
        self.write('first-gen\n')
        follower = LogFollower(self.path)
        self.assertEqual(follower.read_new(), 'first-gen\n')
        rotated = self.path.with_suffix('.1')
        os.replace(self.path, rotated)
        self.write('second-gen\n')
        text = follower.read_new()
        self.assertTrue(text.startswith(ROTATED_MARKER + '\n'))
        self.assertIn('second-gen\n', text)
        self.assertNotIn('first-gen', text)
        self.addCleanup(follower.close)

    def test_partial_utf8_is_replaced_not_raised(self):
        self.write('ok\n'.encode() + b'\xe4\xb8')  # dangling 3-byte prefix
        follower = LogFollower(self.path)
        text = follower.read_new()
        self.assertIn('ok\n', text)
        self.assertIn('\ufffd', text)
        self.addCleanup(follower.close)
