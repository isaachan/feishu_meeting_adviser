"""Session snapshot normalization."""
import unittest

from macos_gui.api import NetworkError
from macos_gui.sessions import normalize_sessions


class NormalizeTests(unittest.TestCase):
    def test_happy_path(self):
        payload = {'user': {'open_id': 'ou_A', 'name': 'Kai Han',
                            'email': 'k@nio.com'},
                   'sessions': [
                       {'session_id': 's-2', 'meeting_no': '222', 'title': 'B',
                        'phase': 'draining', 'started_at': 2.0,
                        'recorder_command_args': None},
                       {'session_id': 's-1', 'meeting_no': '111', 'title': 'A',
                        'phase': 'recording', 'started_at': 1.0,
                        'recorder_command_args': {'session': 's-1', 'event': 'c/e',
                                                  'url': 'ws://h:8400/ingest',
                                                  'token': 'st-x', 'mic': False}}]}
        snapshot = normalize_sessions(payload)
        self.assertEqual(snapshot['user']['open_id'], 'ou_A')
        self.assertEqual([s['session_id'] for s in snapshot['sessions']],
                         ['s-2', 's-1'])
        self.assertEqual(snapshot['sessions'][0]['recorder_command_args'], None)
        self.assertEqual(snapshot['sessions'][1]['recorder_command_args']['token'],
                         'st-x')

    def test_malformed_items_are_dropped(self):
        payload = {'user': {'open_id': 'ou_A'}, 'sessions': [
            {'session_id': 's-ok', 'phase': 'recording',
             'recorder_command_args': None},
            {'session_id': 's-bad-phase', 'phase': 'ended'},
            {'session_id': '', 'phase': 'recording'},
            'garbage',
            {'session_id': 's-bad-args', 'phase': 'recording',
             'recorder_command_args': {'session': 's-bad-args'}}]}
        snapshot = normalize_sessions(payload)
        self.assertEqual([s['session_id'] for s in snapshot['sessions']], ['s-ok'])

    def test_missing_user_or_sessions_raises(self):
        with self.assertRaises(NetworkError):
            normalize_sessions({'sessions': []})
        with self.assertRaises(NetworkError):
            normalize_sessions({'user': {'open_id': 'ou_A'}})
        with self.assertRaises(NetworkError):
            normalize_sessions('nope')


if __name__ == '__main__':
    unittest.main()
