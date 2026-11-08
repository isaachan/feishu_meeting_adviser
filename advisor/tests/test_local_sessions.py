from pathlib import Path
import tempfile
import unittest

from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from feishu_meeting_adviser.storage import StorageError


NOW = 1791849601.0


class LocalSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'adviser.db'
        AdviserStore.initialize(self.path)
        self.store = AdviserStore(self.path)
        self.addCleanup(lambda: self.store.close())

    def test_created_session_has_local_identity_space_and_meeting_row(self):
        session = self.store.create_local_session(title='周会', now=NOW)
        self.assertTrue(session['meeting_id'].startswith('local:'))
        self.assertEqual(session['source'], 'local')
        self.assertEqual(session['phase'], 'recording')
        self.assertEqual(session['join_instance'], session['meeting_id'])
        self.assertEqual(session['started_at'], NOW)
        self.assertEqual(session['joined_at'], NOW)
        self.assertIsNone(session['bot_user_id'])
        self.assertIsNotNone(self.store.meeting(session['meeting_id']))
        second = self.store.create_local_session(now=NOW)
        self.assertNotEqual(session['meeting_id'], second['meeting_id'])
        self.assertEqual([row['meeting_id'] for row in self.store.list_local_sessions()],
                         [session['meeting_id'], second['meeting_id']])

    def test_phase_transitions_are_explicit_and_one_way(self):
        session = self.store.create_local_session(now=NOW)
        mid = session['meeting_id']
        self.assertEqual(self.store.set_local_session_phase(mid, 'draining', now=NOW)['phase'],
                         'draining')
        ended = self.store.set_local_session_phase(mid, 'ended', now=NOW + 5)
        self.assertEqual((ended['phase'], ended['ended_ns']), ('ended', int((NOW + 5) * 1e9)))
        for illegal in (('recording', 'recording'), ('draining', 'recording'),
                        ('ended', 'draining'), ('ended', 'ended')):
            with self.subTest(illegal=illegal):
                with self.assertRaises(StorageError):
                    self.store.set_local_session_phase(mid, illegal[1], now=NOW)
        with self.assertRaises(StorageError):
            self.store.set_local_session_phase(mid, 'paused', now=NOW)

    def test_unknown_session_fails_closed(self):
        self.assertIsNone(self.store.local_session('local:absent'))
        with self.assertRaises(StorageError):
            self.store.require_local_session('local:absent')
        with self.assertRaises(StorageError):
            self.store.set_local_session_phase('local:absent', 'draining', now=NOW)

    def test_recording_session_can_end_directly(self):
        session = self.store.create_local_session(now=NOW)
        ended = self.store.set_local_session_phase(session['meeting_id'], 'ended', now=NOW + 1)
        self.assertEqual(ended['phase'], 'ended')

    def test_title_and_description_are_bounded_strings(self):
        session = self.store.create_local_session(title='t' * 500, description='d' * 30000,
                                                  now=NOW)
        saved = self.store.require_local_session(session['meeting_id'])
        self.assertEqual((len(saved['title']), len(saved['description'])), (200, 20000))
        with self.assertRaises(StorageError):
            self.store.create_local_session(title=None, now=NOW)


if __name__ == '__main__':
    unittest.main()
